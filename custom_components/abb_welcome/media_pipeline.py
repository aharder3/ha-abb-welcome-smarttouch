"""On-demand RTP receiver + RTSP publisher for ABB streaming.

The integration's job:

* SIP-dial the outdoor station when a downstream WebRTC consumer asks
  for the stream
* receive the gateway's RTP/AVP (H.264 video + PCMA audio) on UDP
* push each packet onto an RTSP control connection to HA's bundled
  go2rtc — TCP-interleaved, so video/audio framing is preserved and
  there's no extra muxing latency or "green frame" recovery problem

Closing the pipeline tears everything down: cancel keepalives, BYE
the SIP call, TEARDOWN the RTSP session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import socket
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .intercom_dialer import CallState, Door, IntercomDialer

_LOGGER = logging.getLogger(__name__)

_KEEPALIVE_INTERVAL = 1.0
_RTCP_INTERVAL = 5.0
_PCMA_SILENCE_20MS = b"\xd5" * 160


def _build_rtp_keepalive(seq: int, pt: int = 0) -> bytes:
    return struct.pack("!BBHII", 0x80, pt & 0x7F, seq & 0xFFFF, 0, 0xCAFEBABE)


def _build_rtcp_pli(reporter: int, media: int) -> bytes:
    return struct.pack("!BBHII", 0x81, 206, 2, reporter, media)


def _build_rtcp_rr(reporter: int, source: int, last_seq: int) -> bytes:
    return struct.pack(
        "!BBH IIIIIII",
        0x81, 201, 7,
        reporter & 0xFFFFFFFF,
        source & 0xFFFFFFFF,
        0,
        last_seq & 0xFFFFFFFF,
        0, 0, 0,
    )


def _rtp_payload(data: bytes) -> bytes | None:
    if len(data) < 12 or (data[0] >> 6) != 2:
        return None
    cc = data[0] & 0x0F
    offset = 12 + (cc * 4)
    if len(data) < offset:
        return None
    if data[0] & 0x10:
        if len(data) < offset + 4:
            return None
        extension_words = struct.unpack_from("!H", data, offset + 2)[0]
        offset += 4 + (extension_words * 4)
        if len(data) < offset:
            return None
    end = len(data)
    if data[0] & 0x20:
        padding = data[-1]
        if padding == 0 or padding > end - offset:
            return None
        end -= padding
    return data[offset:end]


def _linear16_to_pcma_sample(sample: int) -> int:
    sample = max(-32768, min(32767, sample)) >> 3
    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 1
    if sample > 0xFFF:
        sample = 0xFFF

    seg_ends = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
    seg = 0
    while seg < 8 and sample > seg_ends[seg]:
        seg += 1

    if seg >= 8:
        aval = 0x7F
    else:
        aval = seg << 4
        if seg < 2:
            aval |= (sample >> 1) & 0x0F
        else:
            aval |= (sample >> seg) & 0x0F
    return aval ^ mask


def _encode_pcm16le_to_pcma(pcm: bytes) -> bytes:
    out = bytearray(len(pcm) // 2)
    pos = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        out[pos] = _linear16_to_pcma_sample(sample)
        pos += 1
    return bytes(out)


class _PCMATalkSender:
    """Continuous PCMA RTP sender for the intercom talkback uplink."""

    def __init__(
        self,
        transport: asyncio.DatagramTransport,
        dest: tuple[str, int],
        *,
        payload_type: int = 8,
        samples_per_packet: int = 160,
        packet_rate: int = 50,
        max_queue_frames: int = 25,
    ) -> None:
        self.transport = transport
        self.dest = dest
        self.payload_type = payload_type
        self.samples_per_packet = samples_per_packet
        self.interval = 1.0 / packet_rate
        self.max_queue_frames = max_queue_frames
        self.ssrc = secrets.randbits(32)
        self.seq = secrets.randbits(16)
        self.timestamp = secrets.randbits(32)
        self.talking = False
        self._marker_next = False
        self._pcm_buffer = bytearray()
        self._queued_pcma: deque[bytes] = deque()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.packets_sent = 0
        self.bytes_sent = 0
        self.frames_received = 0
        self.voice_packets_sent = 0
        self.silence_packets_sent = 0
        self.underrun_silence_packets = 0
        self.dropped_frames = 0

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="abb_pcma_talk_sender")

    async def stop(self) -> None:
        self.stop_talk()
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def start_talk(self) -> None:
        self.talking = True
        self._marker_next = True

    def stop_talk(self) -> None:
        self.talking = False
        self._marker_next = False
        self._pcm_buffer.clear()
        self._queued_pcma.clear()

    async def drain(self, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._queued_pcma and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.interval)

    def feed_pcm16le(self, pcm: bytes) -> int:
        if not self.talking:
            return 0
        self._pcm_buffer.extend(pcm)
        frame_bytes = self.samples_per_packet * 2
        queued = 0
        while len(self._pcm_buffer) >= frame_bytes:
            frame = bytes(self._pcm_buffer[:frame_bytes])
            del self._pcm_buffer[:frame_bytes]
            self._queued_pcma.append(_encode_pcm16le_to_pcma(frame))
            self.frames_received += 1
            queued += 1
            while len(self._queued_pcma) > self.max_queue_frames:
                self._queued_pcma.popleft()
                self.dropped_frames += 1
        return queued

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while not self._stop.is_set():
            payload = _PCMA_SILENCE_20MS
            marker = False
            if self.talking:
                if self._queued_pcma:
                    payload = self._queued_pcma.popleft()
                    marker = self._marker_next
                    self._marker_next = False
                    self.voice_packets_sent += 1
                else:
                    self.underrun_silence_packets += 1
                    self.silence_packets_sent += 1
            else:
                self.silence_packets_sent += 1

            self._send_pcma(payload, marker=marker)
            next_at += self.interval
            sleep_for = next_at - loop.time()
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                next_at = loop.time()

    def _send_pcma(self, payload: bytes, *, marker: bool = False) -> None:
        header = struct.pack(
            "!BBHII",
            0x80,
            (0x80 if marker else 0) | (self.payload_type & 0x7F),
            self.seq & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        )
        self.transport.sendto(header + payload, self.dest)
        self.packets_sent += 1
        self.bytes_sent += len(header) + len(payload)
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + len(payload)) & 0xFFFFFFFF

    def stats(self) -> dict[str, Any]:
        return {
            "talking": self.talking,
            "active": self.active,
            "packets": self.packets_sent,
            "bytes": self.bytes_sent,
            "frames": self.frames_received,
            "voice_packets": self.voice_packets_sent,
            "silence_packets": self.silence_packets_sent,
            "underrun_packets": self.underrun_silence_packets,
            "dropped_frames": self.dropped_frames,
            "queue_frames": len(self._queued_pcma),
            "payload_type": self.payload_type,
            "ssrc": self.ssrc,
            "dest": f"{self.dest[0]}:{self.dest[1]}",
        }


def best_local_ip_for(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _alloc_udp(bind_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((bind_ip, 0))
    s.setblocking(False)
    return s


class _RTPProtocol(asyncio.DatagramProtocol):
    """Receive gateway RTP, optionally rewrite PT, hand off to publisher.

    PT rewriting matters because the ABB gateway negotiates one PT in
    SDP but emits a different PT on the wire.  When we ANNOUNCE our
    SDP to go2rtc we pick a single PT; rewriting incoming packets to
    match keeps go2rtc happy.
    """

    def __init__(
        self,
        on_packet: Callable[[bytes], None],
        rewrite_pt: int | None,
        on_first_packet: Callable[[bytes], None] | None,
        label: str = "rtp",
    ) -> None:
        self._on_packet = on_packet
        self._rewrite_pt = rewrite_pt
        self._on_first_packet = on_first_packet
        self.label = label
        self.transport: asyncio.DatagramTransport | None = None
        self.packets = 0
        self.bytes_received = 0
        self.last_seq = 0
        self.media_ssrc = 0
        self.payload_types: dict[int, int] = {}
        self._rewrites = 0

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets += 1
        self.bytes_received += len(data)
        if len(data) >= 12:
            pt = data[1] & 0x7F
            self.payload_types[pt] = self.payload_types.get(pt, 0) + 1
            self.last_seq = struct.unpack_from("!H", data, 2)[0]
            if self.media_ssrc == 0:
                self.media_ssrc = struct.unpack_from("!I", data, 8)[0]
            if self._rewrite_pt is not None and pt != self._rewrite_pt:
                marker = data[1] & 0x80
                data = (
                    bytes((data[0], marker | (self._rewrite_pt & 0x7F)))
                    + data[2:]
                )
                self._rewrites += 1
        if self.packets == 1 and self._on_first_packet is not None:
            try:
                self._on_first_packet(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("on_first_packet handler raised: %s", err)
        try:
            self._on_packet(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("on_packet handler raised: %s", err)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("RTP datagram error: %s", exc)


@dataclass
class _MediaEndpoints:
    audio: tuple[str, int] | None
    video: tuple[str, int] | None


class StreamSession:
    """One SIP call + RTP receivers + RTSP publisher to go2rtc.

    Responsibilities:

    * dial the gateway, advertise our UDP ports
    * receive video/audio RTP on those ports
    * forward each packet over an RTSP/TCP-interleaved connection to
      go2rtc (which then serves it as ``rtsp://...`` to its consumers
      and via its WebRTC pipeline to the browser)
    """

    VIDEO_SDP_PT = 96
    AUDIO_SDP_PT = 8  # PCMA static PT — no rewrite needed.

    def __init__(
        self,
        *,
        dialer: IntercomDialer,
        door: Door,
        gateway_host: str,
        camera_index: int | None = None,
        incoming_listener: Any | None = None,
        pickup_allowed: Callable[[], bool] | None = None,
        on_call_ended: Callable[[str, str], None] | None = None,
        on_video_packet: Callable[[bytes], None] | None = None,
        on_audio_packet: Callable[[bytes], None] | None = None,
    ) -> None:
        self._dialer = dialer
        self._door = door
        self._gateway_host = gateway_host
        self._camera_index = camera_index
        self._incoming_listener = incoming_listener
        self._pickup_allowed = pickup_allowed
        self._on_call_ended = on_call_ended
        self._on_video_packet = on_video_packet
        self._on_audio_packet = on_audio_packet

        self._media_ip = ""
        self._video_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._video_proto: _RTPProtocol | None = None
        self._audio_proto: _RTPProtocol | None = None
        self._video_transport: asyncio.DatagramTransport | None = None
        self._audio_transport: asyncio.DatagramTransport | None = None
        self._call: CallState | None = None
        self._accepted_incoming_call_id: str | None = None
        self._accepted_incoming_ack_received: bool | None = None
        self._incoming_end_task: asyncio.Task | None = None
        self._outbound_end_task: asyncio.Task | None = None
        self._endpoints = _MediaEndpoints(None, None)
        self._video_codec = "H264/90000"
        self._video_fmtp: str | None = None
        self._remote_audio_pt = 8
        self._remote_video_pt = 102
        self._talk_sender: _PCMATalkSender | None = None

        self._stop = asyncio.Event()
        self._keepalive_task: asyncio.Task | None = None
        self._rtcp_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._call is not None

    @property
    def video_codec(self) -> str:
        return self._video_codec

    @property
    def video_fmtp(self) -> str | None:
        return self._video_fmtp

    @property
    def talkback_ready(self) -> bool:
        return self._talk_sender is not None and self._talk_sender.active

    def talkback_stats(self) -> dict[str, Any]:
        sender = self._talk_sender
        if sender is None:
            return {"active": False, "talking": False}
        return sender.stats()

    def set_packet_handlers(
        self,
        on_video: Callable[[bytes], None] | None,
        on_audio: Callable[[bytes], None] | None,
    ) -> None:
        """Replace the per-packet RTP handlers (used by the RTSP server)."""
        self._on_video_packet = on_video
        self._on_audio_packet = on_audio

    async def open(self) -> None:
        """Dial gateway and start receiving RTP."""
        loop = asyncio.get_running_loop()
        self._accepted_incoming_call_id = None
        self._accepted_incoming_ack_received = None

        self._media_ip = best_local_ip_for(self._gateway_host)
        self._video_sock = _alloc_udp(self._media_ip)
        self._audio_sock = _alloc_udp(self._media_ip)
        offer_audio_port = self._audio_sock.getsockname()[1]
        offer_video_port = self._video_sock.getsockname()[1]

        call = await self._accept_incoming_call_if_pending(
            audio_port=offer_audio_port,
            video_port=offer_video_port,
        )
        accepted_incoming = call is not None
        if call is None:
            _LOGGER.info(
                "[abb] media: dialing gateway for door=%s media_ip=%s "
                "audio_port=%d video_port=%d camera_index=%s",
                self._door.name, self._media_ip,
                offer_audio_port, offer_video_port,
                self._camera_index if self._camera_index is not None else "default",
            )
            call = await self._dialer.dial(
                self._door,
                audio_port=offer_audio_port,
                video_port=offer_video_port,
            )
        else:
            _LOGGER.info(
                "[abb] media: using accepted incoming gateway call for door=%s "
                "call_id=%s ack=%s media_ip=%s audio_port=%d video_port=%d",
                self._door.name,
                call.call_id,
                self._accepted_incoming_ack_received,
                self._media_ip,
                offer_audio_port,
                offer_video_port,
            )
        self._call = call
        if accepted_incoming:
            self._start_incoming_end_watcher(call.call_id)
        else:
            self._start_outbound_end_watcher(call.call_id)
        if self._camera_index is not None and self._accepted_incoming_call_id is None:
            switch_body = f"d:{self._camera_index}"
            _LOGGER.info(
                "[abb] media: selecting camera index %d for door=%s "
                "call_id=%s body=%r",
                self._camera_index, self._door.name, call.call_id, switch_body,
            )
            try:
                response = await self._dialer.send_active_message(
                    switch_body, timeout=3.0
                )
                if response is None:
                    _LOGGER.warning(
                        "[abb] media: camera index %d switch timed out for "
                        "door=%s call_id=%s; continuing stream so logs show RTP",
                        self._camera_index, self._door.name, call.call_id,
                    )
                else:
                    _LOGGER.info(
                        "[abb] media: camera index %d switch accepted for "
                        "door=%s call_id=%s response=%s",
                        self._camera_index, self._door.name, call.call_id,
                        response.start_line,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "[abb] media: camera index %d switch failed for door=%s "
                    "call_id=%s body=%r; continuing default video: %s",
                    self._camera_index, self._door.name, call.call_id,
                    switch_body, err,
                )

        for m in call.answer.medias:
            _LOGGER.info(
                "[abb] media: %s SDP answer camera_index=%s media=%s ip=%s "
                "port=%d pts=%s rtpmap=%s direction=%s",
                self._door.name,
                self._camera_index if self._camera_index is not None else "default",
                m.media, m.connection_ip, m.port,
                m.payload_types, m.rtpmap, m.direction,
            )
            if m.media == "audio" and m.connection_ip and m.port:
                if m.payload_types:
                    self._remote_audio_pt = (
                        8
                        if 8 in m.payload_types
                        else m.payload_types[0]
                    )
                self._endpoints = _MediaEndpoints(
                    audio=(m.connection_ip, m.port),
                    video=self._endpoints.video,
                )
            elif m.media == "video" and m.connection_ip and m.port:
                if m.payload_types:
                    self._remote_video_pt = next(
                        (
                            pt for pt in m.payload_types
                            if "H264" in m.rtpmap.get(pt, "").upper()
                        ),
                        m.payload_types[0],
                    )
                self._endpoints = _MediaEndpoints(
                    audio=self._endpoints.audio,
                    video=(m.connection_ip, m.port),
                )
                if m.payload_types:
                    pt = m.payload_types[0]
                    self._video_codec = m.rtpmap.get(pt, self._video_codec)
                    self._video_fmtp = m.fmtp.get(pt)

        def _video_first(data: bytes) -> None:
            _LOGGER.info(
                "[abb] media: first video RTP packet for %s camera_index=%s "
                "(%d bytes)",
                self._door.name,
                self._camera_index if self._camera_index is not None else "default",
                len(data),
            )
            if self._endpoints.video and self._video_transport:
                ssrc = (
                    struct.unpack_from("!I", data, 8)[0]
                    if len(data) >= 12 else 0
                )
                try:
                    self._video_transport.sendto(
                        _build_rtcp_pli(0xCAFEBABE, ssrc),
                        self._endpoints.video,
                    )
                except OSError:
                    pass

        def _on_video(packet: bytes) -> None:
            cb = self._on_video_packet
            if cb is not None:
                cb(packet)

        def _on_audio(packet: bytes) -> None:
            cb = self._on_audio_packet
            if cb is not None:
                cb(packet)

        self._video_proto = _RTPProtocol(
            on_packet=_on_video,
            rewrite_pt=self.VIDEO_SDP_PT,
            on_first_packet=_video_first,
            label="video",
        )
        self._video_transport, _ = await loop.create_datagram_endpoint(
            lambda: self._video_proto, sock=self._video_sock
        )

        self._audio_proto = _RTPProtocol(
            on_packet=_on_audio,
            rewrite_pt=None,
            on_first_packet=None,
            label="audio",
        )
        self._audio_transport, _ = await loop.create_datagram_endpoint(
            lambda: self._audio_proto, sock=self._audio_sock
        )

        # Punch audio with PCMA PT (8) and video with H.264 PT (102) so
        # the gateway recognises them as legitimate media flows.
        # Keepalives later in ``_keepalive_loop`` use the same PTs.
        if self._endpoints.audio:
            await self._punch(
                self._audio_transport, self._endpoints.audio, pt=self._remote_audio_pt
            )
        if self._endpoints.video:
            await self._punch(
                self._video_transport, self._endpoints.video, pt=self._remote_video_pt
            )

        if self._audio_transport is not None and self._endpoints.audio is not None:
            self._talk_sender = _PCMATalkSender(
                self._audio_transport,
                self._endpoints.audio,
                payload_type=self._remote_audio_pt,
            )
            await self._talk_sender.start()
            _LOGGER.info(
                "[abb] media: talkback RTP leg ready for %s -> %s:%d pt=%d",
                self._door.name,
                self._endpoints.audio[0],
                self._endpoints.audio[1],
                self._remote_audio_pt,
            )

        self._stop.clear()
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(),
            name=f"abb_keepalive_{self._door.station_id}",
        )
        self._rtcp_task = asyncio.create_task(
            self._rtcp_loop(),
            name=f"abb_rtcp_{self._door.station_id}",
        )
        self._stats_task = asyncio.create_task(
            self._stats_loop(),
            name=f"abb_stats_{self._door.station_id}",
        )

    def _start_incoming_end_watcher(self, call_id: str) -> None:
        listener = self._incoming_listener
        wait_end = getattr(listener, "wait_accepted_call_end", None)
        if not callable(wait_end):
            return
        if self._incoming_end_task is not None:
            self._incoming_end_task.cancel()

        async def _watch() -> None:
            try:
                reason = await wait_end(call_id)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("[abb] media: incoming end watcher failed: %s", err)
                return
            if self._call is None or self._call.call_id != call_id:
                return
            _LOGGER.info(
                "[abb] media: incoming call ended by SIP dialog call_id=%s reason=%s",
                call_id,
                reason,
            )
            await self.close()
            self._notify_call_ended(call_id, reason)

        self._incoming_end_task = asyncio.create_task(
            _watch(), name=f"abb_incoming_call_end_{self._door.station_id}"
        )

    def _start_outbound_end_watcher(self, call_id: str) -> None:
        wait_end = getattr(self._dialer, "wait_call_end", None)
        if not callable(wait_end):
            return
        if self._outbound_end_task is not None:
            self._outbound_end_task.cancel()

        async def _watch() -> None:
            try:
                reason = await wait_end(call_id)
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("[abb] media: outbound end watcher failed: %s", err)
                return
            if self._call is None or self._call.call_id != call_id:
                return
            _LOGGER.info(
                "[abb] media: outbound call ended by SIP dialog call_id=%s reason=%s",
                call_id,
                reason,
            )
            await self.close()
            self._notify_call_ended(call_id, reason)

        self._outbound_end_task = asyncio.create_task(
            _watch(), name=f"abb_outbound_call_end_{self._door.station_id}"
        )

    def _notify_call_ended(self, call_id: str, reason: str) -> None:
        if self._on_call_ended is None:
            return
        try:
            self._on_call_ended(call_id, reason)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("[abb] media: call-ended callback failed: %s", err)

    async def _accept_incoming_call_if_pending(
        self,
        *,
        audio_port: int,
        video_port: int,
    ) -> CallState | None:
        """Prefer answering the ringing INVITE over dialing the station again."""
        listener = self._incoming_listener
        station_id = str(self._door.station_id or "").strip()
        if listener is None or not station_id:
            return None
        pending_call_for_station = getattr(listener, "pending_call_for_station", None)
        if (
            callable(pending_call_for_station)
            and pending_call_for_station(station_id)
            and self._pickup_allowed is not None
            and not self._pickup_allowed()
        ):
            _LOGGER.info(
                "[abb] media: refusing incoming pickup for door=%s station=%s "
                "because pickup is disabled",
                self._door.name,
                station_id,
            )
            raise RuntimeError("ABB Welcome incoming-call pickup is disabled")
        accept = getattr(listener, "accept_pending_call", None)
        if not callable(accept):
            return None
        try:
            accepted = await accept(
                station_id=station_id,
                media_ip=self._media_ip,
                audio_port=audio_port,
                video_port=video_port,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "[abb] media: failed to accept incoming call for door=%s "
                "station=%s: %s",
                self._door.name,
                station_id,
                err,
            )
            return None
        if accepted is None:
            return None
        self._accepted_incoming_call_id = accepted.call_id
        self._accepted_incoming_ack_received = accepted.ack_received
        return CallState(
            door=self._door,
            call_id=accepted.call_id,
            local_tag=accepted.local_tag,
            remote_tag=accepted.remote_tag,
            invite_cseq=accepted.invite_cseq,
            request_uri=accepted.caller_uri,
            remote_contact=accepted.remote_contact,
            audio_local_port=accepted.audio_local_port,
            video_local_port=accepted.video_local_port,
            answer=accepted.remote_sdp,
        )

    async def close(self) -> None:
        current_task = asyncio.current_task()
        _LOGGER.info(
            "[abb] media: closing stream for %s "
            "camera_index=%s (video_pkts=%d audio_pkts=%d rewrites=%d)",
            self._door.name,
            self._camera_index if self._camera_index is not None else "default",
            self._video_proto.packets if self._video_proto else 0,
            self._audio_proto.packets if self._audio_proto else 0,
            self._video_proto._rewrites if self._video_proto else 0,
        )
        if (
            self._incoming_end_task is not None
            and self._incoming_end_task is not current_task
        ):
            self._incoming_end_task.cancel()
            try:
                await self._incoming_end_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._incoming_end_task = None
        if (
            self._outbound_end_task is not None
            and self._outbound_end_task is not current_task
        ):
            self._outbound_end_task.cancel()
            try:
                await self._outbound_end_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._outbound_end_task = None

        self._stop.set()
        for t in (self._keepalive_task, self._rtcp_task, self._stats_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._keepalive_task = self._rtcp_task = self._stats_task = None

        if self._talk_sender is not None:
            await self._talk_sender.stop()
            self._talk_sender = None

        for tr in (self._video_transport, self._audio_transport):
            if tr is not None:
                tr.close()
        self._video_transport = self._audio_transport = None
        self._video_sock = self._audio_sock = None
        self._video_proto = self._audio_proto = None

        if self._call is not None:
            try:
                # Scope the hangup to *our* call_id.  Another camera
                # may have replaced the dialer's call between when we
                # dialed and now (the dialer auto-bumps the previous
                # call); without this scope our close() would BYE the
                # successor's call by accident.
                if self._accepted_incoming_call_id is not None:
                    listener = self._incoming_listener
                    hangup = getattr(listener, "hangup_accepted_call", None)
                    if callable(hangup):
                        await hangup(self._accepted_incoming_call_id)
                else:
                    await self._dialer.hangup(call_id=self._call.call_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("[abb] media: hangup failed: %s", err)
            self._call = None
            self._accepted_incoming_call_id = None
            self._accepted_incoming_ack_received = None

        self._endpoints = _MediaEndpoints(None, None)

    def start_talkback(self) -> dict[str, Any]:
        sender = self._talk_sender
        if sender is None or not sender.active:
            raise RuntimeError("talkback is not ready; open the camera stream first")
        sender.start_talk()
        return sender.stats()

    def stop_talkback(self) -> dict[str, Any]:
        sender = self._talk_sender
        if sender is None:
            return {"active": False, "talking": False}
        sender.stop_talk()
        return sender.stats()

    def feed_talkback_pcm16le(self, pcm: bytes) -> dict[str, Any]:
        sender = self._talk_sender
        if sender is None or not sender.active:
            raise RuntimeError("talkback is not ready; open the camera stream first")
        queued = sender.feed_pcm16le(pcm)
        stats = sender.stats()
        stats["queued_frames_now"] = queued
        return stats

    async def send_talkback_tone(
        self,
        *,
        duration_ms: int = 1200,
        frequency_hz: int = 880,
        amplitude: float = 0.35,
    ) -> dict[str, Any]:
        sender = self._talk_sender
        if sender is None or not sender.active:
            raise RuntimeError("talkback is not ready; open the camera stream first")
        duration_ms = max(100, min(int(duration_ms), 5000))
        frequency_hz = max(100, min(int(frequency_hz), 3000))
        amplitude = max(0.0, min(float(amplitude), 1.0))
        sample_rate = 8000
        samples_per_frame = 160
        frames = max(1, duration_ms // 20)

        sender.start_talk()
        try:
            for frame_no in range(frames):
                frame = bytearray(samples_per_frame * 2)
                base = frame_no * samples_per_frame
                for i in range(samples_per_frame):
                    t = (base + i) / sample_rate
                    sample = int(
                        math.sin(2 * math.pi * frequency_hz * t)
                        * amplitude
                        * 32767
                    )
                    frame[i * 2 : i * 2 + 2] = sample.to_bytes(
                        2, "little", signed=True
                    )
                sender.feed_pcm16le(bytes(frame))
                await asyncio.sleep(0.02)
            await sender.drain()
        finally:
            sender.stop_talk()
        return sender.stats()

    async def _punch(
        self,
        transport: asyncio.DatagramTransport,
        dest: tuple[str, int],
        *,
        pt: int = 0,
    ) -> None:
        for i in range(6):
            try:
                transport.sendto(_build_rtp_keepalive(i, pt=pt), dest)
            except OSError:
                break

    async def _keepalive_loop(self) -> None:
        seq = 6
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_KEEPALIVE_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            for transport, dest, pt in (
                (
                    None if self._talk_sender is not None else self._audio_transport,
                    self._endpoints.audio,
                    self._remote_audio_pt,
                ),
                (self._video_transport, self._endpoints.video, self._remote_video_pt),
            ):
                if transport is None or dest is None:
                    continue
                try:
                    transport.sendto(_build_rtp_keepalive(seq, pt=pt), dest)
                except OSError:
                    pass
                seq = (seq + 1) & 0xFFFF

    async def _rtcp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RTCP_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            vp = self._video_proto
            if (
                vp is None
                or self._video_transport is None
                or self._endpoints.video is None
                or vp.media_ssrc == 0
            ):
                continue
            try:
                self._video_transport.sendto(
                    _build_rtcp_rr(0xCAFEBABE, vp.media_ssrc, vp.last_seq),
                    self._endpoints.video,
                )
            except OSError:
                pass

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass
            vp = self._video_proto
            ap = self._audio_proto
            _LOGGER.info(
                "[abb] media stats %s camera_index=%s: video pkts=%d pts=%s "
                "rewrites=%d audio pkts=%d",
                self._door.name,
                self._camera_index if self._camera_index is not None else "default",
                vp.packets if vp else 0,
                dict(vp.payload_types) if vp else {},
                vp._rewrites if vp else 0,
                ap.packets if ap else 0,
            )
            if self._talk_sender is not None:
                _LOGGER.info(
                    "[abb] talkback stats %s camera_index=%s: %s",
                    self._door.name,
                    self._camera_index if self._camera_index is not None else "default",
                    self._talk_sender.stats(),
                )
