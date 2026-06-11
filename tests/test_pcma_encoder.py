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


if __name__ == "__main__":
    test_table_matches_scalar_for_every_sample()
    test_encode_matches_reference_full_range()
    test_encode_matches_reference_varied_buffers()
    test_encode_ignores_trailing_odd_byte()
    test_silence_constant_matches_zero_sample()
    print(f"OK  numpy_fast_path={'yes' if mp._ALAW_LUT is not None else 'no (fallback)'}")
