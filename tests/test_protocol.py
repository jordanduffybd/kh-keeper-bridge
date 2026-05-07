"""Frame encode/decode + Reader primitive tests.

The wire protocol is null-delimited:
    serial\\0command\\0subcommand\\0txid\\0payload\\0

These tests guard the round-trip — if encode/decode drift, every command
silently fails to be recognized by the device or vice versa.
"""
from __future__ import annotations

import struct

from kh_keeper_bridge.kh_keeper_bridge import (
    Reader,
    SCALE,
    decode_frame,
    encode_frame,
)


def test_encode_decode_round_trip():
    """Round-trip preserves the four header fields. The decoded payload
    includes the trailing null the encoder appends — an asymmetry that
    exists by design (see test_decode_returns_payload_with_trailing_nul).
    """
    encoded = encode_frame("ABC123", "khSet", "settings", "tx-1", b"\x01\x02\x03")
    serial, command, subcommand, txid, payload = decode_frame(encoded)
    assert serial == "ABC123"
    assert command == "khSet"
    assert subcommand == "settings"
    assert txid == "tx-1"
    # Trailing NUL preserved by decoder — see test below.
    assert payload == b"\x01\x02\x03\x00"


def test_encode_empty_payload():
    """Most commands have empty payloads (e.g. takeNow, cancel,
    measurePh). The encoder still appends the trailing null."""
    encoded = encode_frame("S", "c", "sc", "t", b"")
    assert encoded == b"S\x00c\x00sc\x00t\x00\x00"


def test_decode_returns_payload_with_trailing_nul():
    """The decoder takes everything after the 4th NUL as payload —
    including the trailing NUL the encoder appends. All current
    handlers slice from the front (`payload[:4]` or fixed-length reads)
    so the extra NUL is harmless in practice. Documenting here so that
    a future "make encode/decode symmetric" refactor doesn't silently
    break callers that depend on the current shape."""
    payload = b"\x00\x0a\xae\x60\x00"
    encoded = encode_frame("S", "khCommand", "empty", "t", payload)
    _, _, _, _, decoded_payload = decode_frame(encoded)
    # The decoded payload is the original PLUS the trailing NUL framer.
    assert decoded_payload == payload + b"\x00"
    # Internal nulls in the payload are still safe — the decoder only
    # consumes the first 4 NUL-delimited header fields.
    assert decoded_payload[:5] == payload


def test_reader_u8():
    r = Reader(b"\x05\x42")
    assert r.u8() == 5
    assert r.u8() == 0x42


def test_reader_u16_big_endian():
    r = Reader(b"\x01\x02")
    assert r.u16() == 0x0102


def test_reader_i32_signed():
    r = Reader(struct.pack(">i", -12345))
    assert r.i32() == -12345


def test_reader_fixed_point_uses_scale():
    """fixed() returns i32 / SCALE. Used for KH (dKH), pH, mL etc."""
    r = Reader(struct.pack(">i", int(8.85 * SCALE)))
    assert abs(r.fixed() - 8.85) < 0.0001


def test_reader_advances_offset_across_calls():
    r = Reader(struct.pack(">i", 100) + struct.pack(">i", 200))
    assert r.i32() == 100
    assert r.i32() == 200


def test_reader_remaining_tracks_consumed_bytes():
    r = Reader(b"\x01\x02\x03\x04")
    assert r.remaining() == 4
    r.u8()
    assert r.remaining() == 3
    r.u16()
    assert r.remaining() == 1
