"""Equivalence tests for the vectorised PCMA (A-law) talkback encoder.

The encoder was rewritten from a per-sample Python loop to a precomputed
lookup table (with an optional numpy fast path) to keep talkback encoding off
the event loop's hot path.  These tests prove the new implementation produces
byte-for-byte identical output to an independent reference for every possible
sample value and a spread of realistic buffers — if it didn't, talkback audio
would be silently corrupted.

The module is loaded directly from its file so the suite runs without Home
Assistant (or numpy) installed: importing ``abb_welcome`` normally would pull
in ``homeassistant``/``requests``/``voluptuous``.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "abb_welcome"


def _load_media_pipeline() -> types.ModuleType:
    """Load abb_welcome.media_pipeline without executing the package __init__."""
    pkg = types.ModuleType("abb_welcome")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules.setdefault("abb_welcome", pkg)

    for name in ("intercom_dialer", "media_pipeline"):
        full = f"abb_welcome.{name}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules["abb_welcome.media_pipeline"]


mp = _load_media_pipeline()


def _reference_encode(pcm: bytes) -> bytes:
    """The original per-sample implementation, kept here as ground truth."""
    out = bytearray(len(pcm) // 2)
    pos = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        out[pos] = mp._linear16_to_pcma_sample(sample)
        pos += 1
    return bytes(out)


def test_table_matches_scalar_for_every_sample() -> None:
    """Every one of the 65 536 possible inputs maps identically."""
    for sample in range(-32768, 32768):
        assert mp._ALAW_TABLE[sample + 32768] == mp._linear16_to_pcma_sample(sample)


def test_encode_matches_reference_full_range() -> None:
    """Encoding a buffer covering all sample values matches the reference."""
    pcm = struct.pack("<%dh" % 65536, *range(-32768, 32768))
    assert mp._encode_pcm16le_to_pcma(pcm) == _reference_encode(pcm)


def test_encode_matches_reference_varied_buffers() -> None:
    cases = [
        b"",
        b"\x00\x00",
        struct.pack("<4h", -32768, -1, 0, 32767),
        struct.pack("<6h", 1, 2, 3, -3, -2, -1),
        bytes((i * 37) % 256 for i in range(320)),  # 20 ms frame of noise
        bytes((i * 211) % 256 for i in range(3200)),  # 100 ms chunk
    ]
    for pcm in cases:
        assert mp._encode_pcm16le_to_pcma(pcm) == _reference_encode(pcm), pcm[:8]


def test_encode_ignores_trailing_odd_byte() -> None:
    """An odd trailing byte is dropped, exactly like the reference loop."""
    pcm = struct.pack("<3h", 100, -200, 300) + b"\x7f"
    encoded = mp._encode_pcm16le_to_pcma(pcm)
    assert encoded == _reference_encode(pcm)
    assert len(encoded) == 3


def test_silence_constant_matches_zero_sample() -> None:
    """The hard-coded silence byte equals the A-law encoding of sample 0."""
    assert mp._PCMA_SILENCE_20MS == bytes([mp._linear16_to_pcma_sample(0)]) * 160



def test_abb_aes_rtp_talkback_matches_original_packet_shape() -> None:
    """AES RTP uses a 2-byte clear length and encrypted 16-byte blocks."""
    class FakeTransport:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def sendto(self, packet: bytes, dest: tuple[str, int]) -> None:
            self.sent.append(packet)

    transport = FakeTransport()
    key = bytes(range(16))
    sender = mp._PCMATalkSender(
        transport, ("127.0.0.1", 9000), encrypt_key=key
    )
    pcma = b"\xd5" * 160
    initial_timestamp = sender.timestamp
    sender._send_pcma(pcma)
    packet = transport.sent[0]
    assert len(packet) == 174  # 12 RTP + 2 length + 160 AES
    assert packet[:2] == b"\x80\x08"
    assert packet[12:14] == b"\x00\xa0"
    assert packet[14:30] == packet[30:46]  # ECB pattern for A-law silence
    assert packet[14:30] != pcma[:16]
    assert mp._decrypt_abb_media_rtp(packet, key) == packet[:12] + pcma
    assert sender.timestamp == (initial_timestamp + 160) & 0xFFFFFFFF
    assert sender.stats()["encrypted"] is True


def test_abb_aes_rtp_zero_padding_roundtrip() -> None:
    key = bytes(range(16))
    for n in (1, 79, 80, 81, 159, 160, 161):
        pcma = bytes([0xD5]) * n
        payload = mp._encrypt_abb_media_payload(pcma, key)
        assert len(payload) == 2 + ((n + 15) // 16) * 16
        packet = struct.pack("!BBHII", 0x80, 8, 123, 456, 789) + payload
        assert mp._decrypt_abb_media_rtp(packet, key) == packet[:12] + pcma


def test_missing_sdp_key_drops_encrypted_audio_not_plain_audio() -> None:
    packets: list[bytes] = []
    receiver = mp._RTPProtocol(
        packets.append, rewrite_pt=None, on_first_packet=None, label="audio"
    )
    header = struct.pack("!BBHII", 0x80, 8, 123, 456, 789)
    encrypted = header + mp._encrypt_abb_media_payload(
        b"\xd5" * 160, bytes(range(16))
    )
    receiver.datagram_received(encrypted, ("127.0.0.1", 9000))
    assert packets == []
    assert receiver.missing_crypto_dropped == 1
    receiver.datagram_received(header + b"\xd5" * 160, ("127.0.0.1", 9000))
    assert packets == [header + b"\xd5" * 160]
    receiver.datagram_received(b"\x00" * 20, ("127.0.0.1", 9000))
    assert receiver.invalid_rtp_dropped == 1



if __name__ == "__main__":
    test_table_matches_scalar_for_every_sample()
    test_encode_matches_reference_full_range()
    test_encode_matches_reference_varied_buffers()
    test_encode_ignores_trailing_odd_byte()
    test_silence_constant_matches_zero_sample()
    test_abb_aes_rtp_talkback_matches_original_packet_shape()
    test_abb_aes_rtp_zero_padding_roundtrip()
    test_missing_sdp_key_drops_encrypted_audio_not_plain_audio()
    print(f"OK  numpy_fast_path={'yes' if mp._ALAW_LUT is not None else 'no (fallback)'}")
