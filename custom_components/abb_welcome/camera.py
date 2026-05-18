"""Camera entities for ABB Welcome outdoor stations.

Each camera owns:

* a tiny RTSP server (per-entity, stable port for the entity's
  lifetime) — go2rtc connects to it as a client when a downstream
  WebRTC consumer wants the stream
* a :class:`StreamSession` that SIP-dials the gateway and forwards
  RTP packets via the RTSP server's TCP-interleaved framing

Streaming is gated and lazy:

* the per-gateway *armed* flag (see :mod:`streaming_state`) decides
  whether streaming is permitted right now — accidentally opening the
  intercom locks out the whole building, so it stays off by default
* on the first RTSP DESCRIBE from go2rtc (which fires when a browser/
  HomeKit consumer asks for the stream) we dial the gateway and
  return the gateway's SDP
* on PLAY we start forwarding RTP through the same TCP socket
* on the *last* TEARDOWN (or connection close) we BYE the gateway
  after a brief grace period so the building's intercom is freed
"""

from __future__ import annotations

import asyncio
import logging
import re
from http import HTTPStatus
from urllib.parse import urljoin

from aiohttp import ClientError, ClientSession, ClientTimeout
from go2rtc_client.ws import (
    Go2RtcWsClient,
    WebRTCAnswer as Go2RTCAnswer,
    WebRTCCandidate as Go2RTCCandidate,
    WebRTCOffer as Go2RTCOffer,
    WsError as Go2RTCWsError,
)
from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    Camera,
    CameraCapabilities,
    CameraEntityFeature,
    StreamType,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .intercom_dialer import Door, IntercomDialer
from .media_pipeline import StreamSession
from .rtsp_server import (
    AUDIO_RTP_CHANNEL,
    VIDEO_RTP_CHANNEL,
    RtspServer,
    RtspSession,
)
from .streaming_state import is_armed, signal_armed_changed

_LOGGER = logging.getLogger(__name__)

_GO2RTC_DOMAIN = "go2rtc"
_REQUEST_TIMEOUT = ClientTimeout(total=10)
_TEARDOWN_GRACE_SECONDS = 2.0
_DEFAULT_CAMERA_COUNT = 3


def _safe_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return key or "station"


def _raw_int(raw: dict, *keys: str) -> int | None:
    for key in keys:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        return parsed if parsed > 0 else None
    return None


def _camera_count_from_raw(raw: dict) -> int:
    explicit = _raw_int(raw, "camera_count", "sub_camera_count", "cameras")
    if explicit is not None:
        return explicit
    return _DEFAULT_CAMERA_COUNT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    sip_user = entry.data.get("sip_username")
    sip_pass = entry.data.get("sip_password")
    sip_domain = entry.data.get("sip_domain")
    gw_ip = entry.data.get("gateway_ip")
    raw_doors = entry.data.get("doors", []) or []
    if not (sip_user and sip_pass and sip_domain and gw_ip and raw_doors):
        return

    dialer = IntercomDialer(
        host=gw_ip, username=sip_user, password=sip_pass, domain=sip_domain
    )
    data["intercom_dialer"] = dialer

    async def _close_dialer(*_args) -> None:
        await dialer.close()

    entry.async_on_unload(_close_dialer)

    gateway_uuid = entry.data.get("gateway_uuid", "unknown")
    cameras: list[Camera] = []
    for raw in raw_doors:
        if not isinstance(raw, dict):
            continue
        addr = raw.get("address", "")
        if not addr:
            continue
        door = Door(
            name=raw.get("name") or addr,
            address=addr,
            station_id=str(raw.get("station_id", "")),
        )
        camera_count = _camera_count_from_raw(raw)
        station_type = str(raw.get("type", "")).strip()
        can_unlock = raw.get("can_unlock")
        _LOGGER.info(
            "[abb] camera setup: station name=%s address=%s station_id=%s "
            "type=%s can_unlock=%s camera_count=%d",
            door.name, door.address, door.station_id,
            station_type or "unknown", can_unlock, camera_count,
        )
        for camera_index in range(1, camera_count + 1):
            exposed_index = camera_index if camera_count > 1 else None
            cameras.append(
                ABBWelcomeCamera(
                    hass=hass,
                    entry_id=entry.entry_id,
                    dialer=dialer,
                    door=door,
                    gateway_uuid=gateway_uuid,
                    camera_index=exposed_index,
                    camera_count=camera_count,
                    station_type=station_type,
                    can_unlock=can_unlock,
                )
            )
            _LOGGER.info(
                "[abb] camera setup: added entity door=%s camera_index=%s "
                "switch_body=%s",
                door.name,
                exposed_index if exposed_index is not None else "default",
                f"d:{exposed_index}" if exposed_index is not None else "none",
            )
    if cameras:
        async_add_entities(cameras)


def _go2rtc_url(hass: HomeAssistant) -> str | None:
    bucket = hass.data.get(_GO2RTC_DOMAIN)
    url = getattr(bucket, "url", bucket)
    if isinstance(url, str) and url:
        return url.rstrip("/") + "/"
    return None


def _go2rtc_session(hass: HomeAssistant) -> ClientSession:
    bucket = hass.data.get(_GO2RTC_DOMAIN)
    session = getattr(bucket, "session", None)
    if session is not None:
        return session
    return async_get_clientsession(hass)


class ABBWelcomeCamera(Camera):
    """Camera entity backed by per-entity RTSP server + lazy SIP dial."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_icon = "mdi:doorbell-video"

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entry_id: str,
        dialer: IntercomDialer,
        door: Door,
        gateway_uuid: str,
        camera_index: int | None,
        camera_count: int,
        station_type: str,
        can_unlock: bool | str | int | None,
    ) -> None:
        super().__init__()
        self.hass = hass
        self._entry_id = entry_id
        self._dialer = dialer
        self._door = door
        self._gateway_uuid = gateway_uuid
        self._camera_index = camera_index
        self._camera_count = camera_count
        self._station_type = station_type
        self._can_unlock = can_unlock
        station_key = _safe_key(door.station_id or door.address)
        camera_suffix = (
            f"_cam{camera_index}"
            if camera_index is not None and camera_index > 1
            else ""
        )
        self._attr_name = (
            f"{door.name} Camera {camera_index}"
            if camera_index is not None and camera_index > 1
            else door.name
        )
        self._attr_unique_id = f"{gateway_uuid}_camera_{station_key}{camera_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, gateway_uuid)},
            name="ABB Welcome Gateway",
            manufacturer="ABB / Busch-Jaeger",
            model="IP Gateway (MRANGE)",
        )
        self._stream_name = f"abb_{station_key}{camera_suffix}"

        self._session = StreamSession(
            dialer=dialer,
            door=door,
            gateway_host=dialer.host,
            camera_index=camera_index,
        )
        self._rtsp = RtspServer(
            host="127.0.0.1",
            on_describe=self._on_rtsp_describe,
            on_play=self._on_rtsp_play,
            on_teardown=self._on_rtsp_teardown,
        )

        # WebRTC session bookkeeping (one Go2RtcWsClient per HA frontend
        # session).
        self._ws_clients: dict[str, Go2RtcWsClient] = {}
        self._ws_pending_candidates: dict[str, list[str]] = {}

        self._stream_lock = asyncio.Lock()
        self._unsub_armed: callable | None = None

    @property
    def camera_capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(frontend_stream_types={StreamType.WEB_RTC})

    @property
    def extra_state_attributes(self) -> dict[str, str | int | bool]:
        attrs: dict[str, str | int | bool] = {
            "go2rtc_stream": self._stream_name,
            "rtsp_url": self._rtsp.url if self._rtsp.port else "",
            "armed": is_armed(self.hass, self._entry_id),
            "ws_sessions": len(self._ws_clients),
            "rtsp_sessions": self._rtsp.session_count,
            "stream_active": self._session.active,
            "camera_count": self._camera_count,
            "camera_interface": self._camera_count > 1,
        }
        if self._camera_index is not None:
            attrs["camera_index"] = self._camera_index
            attrs["camera_switch_body"] = f"d:{self._camera_index}"
        if self._station_type:
            attrs["station_type"] = self._station_type
        if self._can_unlock is not None:
            attrs["can_unlock"] = self._can_unlock
        return attrs

    # ------------------------------------------------------------------ #
    # entity lifecycle                                                   #
    # ------------------------------------------------------------------ #

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_armed = async_dispatcher_connect(
            self.hass,
            signal_armed_changed(self._entry_id),
            self._on_armed_changed,
        )
        await self._rtsp.start()
        _LOGGER.info(
            "[abb] camera %s: RTSP server ready url=%s stream=%s "
            "camera_index=%s camera_count=%d",
            self._attr_name, self._rtsp.url, self._stream_name,
            self._camera_index if self._camera_index is not None else "default",
            self._camera_count,
        )
        await self._register_with_go2rtc()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_armed is not None:
            self._unsub_armed()
            self._unsub_armed = None

        for ws in list(self._ws_clients.values()):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_clients.clear()
        self._ws_pending_candidates.clear()

        await self._rtsp.stop()
        if self._session.active:
            await self._session.close()
        await self._unregister_from_go2rtc()
        await super().async_will_remove_from_hass()

    @callback
    def _on_armed_changed(self) -> None:
        if not is_armed(self.hass, self._entry_id):
            if self._rtsp.session_count or self._session.active:
                _LOGGER.info(
                    "[abb] camera %s: armed flipped off, force-closing "
                    "camera_index=%s rtsp_sessions=%d active=%s",
                    self._attr_name,
                    self._camera_index
                    if self._camera_index is not None
                    else "default",
                    self._rtsp.session_count,
                    self._session.active,
                )
                self.hass.async_create_task(self._force_teardown())
        self.async_write_ha_state()

    async def _force_teardown(self) -> None:
        await self._rtsp.stop()
        async with self._stream_lock:
            if self._session.active:
                await self._session.close()
        # Restart server so future connections still work.
        await self._rtsp.start()
        await self._register_with_go2rtc()

    # ------------------------------------------------------------------ #
    # go2rtc registration                                                #
    # ------------------------------------------------------------------ #

    async def _register_with_go2rtc(self) -> None:
        base_url = _go2rtc_url(self.hass)
        if base_url is None:
            _LOGGER.warning(
                "[abb] camera %s: HA-bundled go2rtc not available — WebRTC disabled",
                self._door.name,
            )
            return
        session = _go2rtc_session(self.hass)
        src = self._rtsp.url
        attempts: tuple[tuple[str, dict[str, str]], ...] = (
            ("put", {"name": self._stream_name, "src": src}),
            ("patch", {"name": self._stream_name, "src": src}),
        )
        for method, params in attempts:
            try:
                request = getattr(session, method)
                async with request(
                    urljoin(base_url, "api/streams"),
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                ) as resp:
                    await resp.read()
                    if resp.status in (
                        HTTPStatus.OK,
                        HTTPStatus.CREATED,
                        HTTPStatus.NO_CONTENT,
                    ):
                        _LOGGER.info(
                            "[abb] camera %s: registered go2rtc stream %s -> %s "
                            "camera_index=%s",
                            self._attr_name, self._stream_name, src,
                            self._camera_index
                            if self._camera_index is not None
                            else "default",
                        )
                        return
            except (ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "[abb] camera %s: go2rtc %s registration error: %s",
                    self._door.name, method.upper(), err,
                )

    async def _unregister_from_go2rtc(self) -> None:
        base_url = _go2rtc_url(self.hass)
        if base_url is None:
            return
        session = _go2rtc_session(self.hass)
        for params in (
            {"name": self._stream_name},
            {"src": self._stream_name},
        ):
            try:
                async with session.delete(
                    urljoin(base_url, "api/streams"),
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                ) as resp:
                    await resp.read()
                    if resp.status in (
                        HTTPStatus.OK,
                        HTTPStatus.NO_CONTENT,
                    ):
                        return
            except (ClientError, TimeoutError) as err:
                _LOGGER.debug(
                    "[abb] camera %s: go2rtc DELETE %s failed: %s",
                    self._door.name, params, err,
                )

    # ------------------------------------------------------------------ #
    # RTSP server callbacks                                              #
    # ------------------------------------------------------------------ #

    async def _on_rtsp_describe(self) -> str | None:
        _LOGGER.info(
            "[abb] camera %s: DESCRIBE armed=%s active=%s rtsp_sessions=%d "
            "camera_index=%s camera_count=%d switch_body=%s",
            self._attr_name,
            is_armed(self.hass, self._entry_id),
            self._session.active,
            self._rtsp.session_count,
            self._camera_index if self._camera_index is not None else "default",
            self._camera_count,
            f"d:{self._camera_index}" if self._camera_index is not None else "none",
        )
        if not is_armed(self.hass, self._entry_id):
            _LOGGER.info(
                "[abb] camera %s: refusing DESCRIBE (not armed) camera_index=%s",
                self._attr_name,
                self._camera_index if self._camera_index is not None else "default",
            )
            return None
        async with self._stream_lock:
            if not self._session.active:
                try:
                    await self._session.open()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception(
                        "[abb] camera %s: failed to open stream session "
                        "camera_index=%s: %s",
                        self._attr_name,
                        self._camera_index
                        if self._camera_index is not None
                        else "default",
                        err,
                    )
                    return None
        codec = self._session.video_codec or "H264/90000"
        # WebRTC matcher needs a profile-level-id; ABB doesn't include
        # one in its SDP answer (we'd just have packetization-mode), and
        # without it go2rtc 1.9.9 logs "codecs not matched".  42e01f =
        # Constrained Baseline @ Level 3.1 — a safe lowest-common-
        # denominator that essentially all WebRTC clients accept.
        fmtp = self._session.video_fmtp or "packetization-mode=1;profile-level-id=42e01f"
        # PCMA is one of the codecs WebRTC keeps in its standard menu
        # (RFC 7874) so browsers offer it alongside Opus — verified
        # locally with go2rtc 1.9.9: it passthroughs PCMA in the
        # WebRTC answer when the browser advertises it (no transcode
        # needed).  We expose it as a separate track here.
        sdp_lines = [
            "v=0",
            "o=- 0 0 IN IP4 127.0.0.1",
            "s=ABB",
            "c=IN IP4 127.0.0.1",
            "t=0 0",
            "a=control:*",
            "m=video 0 RTP/AVP 96",
            f"a=rtpmap:96 {codec}",
            f"a=fmtp:96 {fmtp}",
            "a=control:trackID=0",
            "m=audio 0 RTP/AVP 8",
            "a=rtpmap:8 PCMA/8000",
            "a=control:trackID=1",
            "",
        ]
        _LOGGER.info(
            "[abb] camera %s: DESCRIBE returning SDP codec=%s fmtp=%s "
            "camera_index=%s",
            self._attr_name, codec, fmtp,
            self._camera_index if self._camera_index is not None else "default",
        )
        return "\r\n".join(sdp_lines) + "\r\n"

    async def _on_rtsp_play(self, sess: RtspSession) -> None:
        # Wire StreamSession's RTP packets to the RTSP TCP-interleaved push.
        def _on_video(packet: bytes) -> None:
            sess.push_rtp(VIDEO_RTP_CHANNEL, packet)

        def _on_audio(packet: bytes) -> None:
            sess.push_rtp(AUDIO_RTP_CHANNEL, packet)

        self._session.set_packet_handlers(on_video=_on_video, on_audio=_on_audio)
        _LOGGER.info(
            "[abb] camera %s: PLAY started, forwarding RTP to RTSP client "
            "camera_index=%s rtsp_sessions=%d",
            self._attr_name,
            self._camera_index if self._camera_index is not None else "default",
            self._rtsp.session_count,
        )

    async def _on_rtsp_teardown(self, sess: RtspSession) -> None:
        _LOGGER.info(
            "[abb] camera %s: TEARDOWN camera_index=%s remaining_sessions=%d "
            "active=%s",
            self._attr_name,
            self._camera_index if self._camera_index is not None else "default",
            self._rtsp.session_count,
            self._session.active,
        )
        if self._rtsp.session_count == 0:
            self._session.set_packet_handlers(on_video=None, on_audio=None)
            self.hass.async_create_task(self._delayed_session_close())

    async def _delayed_session_close(self) -> None:
        try:
            await asyncio.sleep(_TEARDOWN_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        if self._rtsp.session_count > 0:
            return
        async with self._stream_lock:
            if self._rtsp.session_count > 0:
                return
            if self._session.active:
                await self._session.close()

    # ------------------------------------------------------------------ #
    # stream_source for non-WebRTC consumers                             #
    # ------------------------------------------------------------------ #

    async def stream_source(self) -> str | None:
        if _go2rtc_url(self.hass) is None:
            return None
        return f"rtsp://127.0.0.1:18554/{self._stream_name}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return None

    # ------------------------------------------------------------------ #
    # WebRTC offer / candidate forwarding to go2rtc                      #
    # ------------------------------------------------------------------ #

    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        _LOGGER.info(
            "[abb] camera %s: WebRTC offer session_id=%s armed=%s "
            "camera_index=%s stream=%s",
            self._attr_name,
            session_id,
            is_armed(self.hass, self._entry_id),
            self._camera_index if self._camera_index is not None else "default",
            self._stream_name,
        )
        if not is_armed(self.hass, self._entry_id):
            send_message(
                WebRTCError(
                    code="abb_streaming_disarmed",
                    message=(
                        "ABB Welcome streaming is disarmed. "
                        "Toggle the streaming switch on, or wait for an inbound ring."
                    ),
                )
            )
            return

        base_url = _go2rtc_url(self.hass)
        if base_url is None:
            _LOGGER.warning(
                "[abb] camera %s: WebRTC offer failed, go2rtc unavailable "
                "camera_index=%s",
                self._attr_name,
                self._camera_index if self._camera_index is not None else "default",
            )
            send_message(
                WebRTCError(
                    code="go2rtc_unavailable",
                    message="HA-bundled go2rtc is not available",
                )
            )
            return

        # Refresh registration in case it got dropped (HA-managed go2rtc
        # restarts may wipe streams).
        await self._register_with_go2rtc()

        ws = Go2RtcWsClient(
            _go2rtc_session(self.hass),
            base_url.rstrip("/"),
            source=self._stream_name,
        )

        @callback
        def _on_message(message) -> None:
            match message:
                case Go2RTCCandidate():
                    send_message(
                        WebRTCCandidate(RTCIceCandidateInit(message.candidate))
                    )
                case Go2RTCAnswer():
                    send_message(WebRTCAnswer(message.sdp))
                case Go2RTCWsError():
                    send_message(
                        WebRTCError(
                            code="go2rtc_error",
                            message=str(message.error),
                        )
                    )

        ws.subscribe(_on_message)
        self._ws_clients[session_id] = ws

        try:
            config = self.async_get_webrtc_client_configuration()
            await ws.send(
                Go2RTCOffer(offer_sdp, config.configuration.ice_servers)
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception(
                "[abb] camera %s: WebRTC offer forward failed session_id=%s "
                "camera_index=%s: %s",
                self._attr_name,
                session_id,
                self._camera_index if self._camera_index is not None else "default",
                err,
            )
            self._ws_clients.pop(session_id, None)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            send_message(
                WebRTCError(
                    code="go2rtc_offer_failed", message=str(err),
                )
            )
            return

        for c in self._ws_pending_candidates.pop(session_id, []):
            try:
                await ws.send(Go2RTCCandidate(c))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("[abb] queued candidate forward failed: %s", err)

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        ws = self._ws_clients.get(session_id)
        if ws is None:
            self._ws_pending_candidates.setdefault(session_id, []).append(
                candidate.candidate
            )
            return
        try:
            await ws.send(Go2RTCCandidate(candidate.candidate))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("[abb] forward candidate failed: %s", err)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        ws = self._ws_clients.pop(session_id, None)
        self._ws_pending_candidates.pop(session_id, None)
        if ws is not None:
            self.hass.async_create_task(ws.close())
