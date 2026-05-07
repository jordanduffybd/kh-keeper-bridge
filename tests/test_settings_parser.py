"""Settings frame parser tests.

`parse_settings` decodes a binary `khRefresh/settings` payload from the
device. The parser is tolerant of truncation past the documented prefix
(some firmware versions don't emit the trailing fields) — these tests
guard the prefix that EVERY firmware sends, plus the trailing-field
optionality.
"""
from __future__ import annotations

import struct

from kh_keeper_bridge.kh_keeper_bridge import SCALE, parse_settings


def _f(value: float) -> bytes:
    """Encode value as 4-byte fixed-point i32."""
    return struct.pack(">i", int(round(value * SCALE)))


def _build_minimal_settings(
    *, alarm_low=7.5, alarm_high=8.8, state=0, percent=0,
    interval=0, reagent_ml=120.0, reagent_low=False,
    history_count=0,
) -> bytes:
    """Build the smallest valid settings payload — alarms + state +
    interval + reagent + empty history. Trailing optional fields
    (calibration, next_test, adjustment block, water counters) omitted
    so the parser's truncation handling is exercised."""
    return (
        _f(alarm_low) + _f(alarm_high)
        + bytes([state, percent, interval])
        + _f(reagent_ml) + bytes([1 if reagent_low else 0, history_count])
    )


def test_parse_alarms_round_trip():
    payload = _build_minimal_settings(alarm_low=7.5, alarm_high=8.9)
    out = parse_settings(payload)
    assert out["alarm_kh_low"] == 7.5
    assert out["alarm_kh_high"] == 8.9


def test_parse_state_text_lookup():
    """State code 0 = Idle (per STATE_TEXT)."""
    payload = _build_minimal_settings(state=0)
    out = parse_settings(payload)
    assert out["state"] == "Idle"
    assert out["state_code"] == 0


def test_parse_state_unknown_code_is_labeled():
    """Unknown state codes don't crash — they're labeled 'Unknown (N)'."""
    payload = _build_minimal_settings(state=99)
    out = parse_settings(payload)
    assert "Unknown" in out["state"]
    assert "99" in out["state"]


def test_parse_reagent_low_flag():
    payload = _build_minimal_settings(reagent_low=True)
    out = parse_settings(payload)
    assert out["reagent_low"] is True

    payload = _build_minimal_settings(reagent_low=False)
    out = parse_settings(payload)
    assert out["reagent_low"] is False


def test_parse_no_history_yields_none_kh_and_ph():
    """Empty history (fresh device, never tested) → kh/ph/last_test_*
    are explicitly None, not missing keys (so downstream code can
    assume the keys exist)."""
    payload = _build_minimal_settings(history_count=0)
    out = parse_settings(payload)
    assert out["kh"] is None
    assert out["ph"] is None
    assert out["last_test_time"] is None
    assert out["last_test_alert"] is None
    assert out["history"] == []


def test_parse_history_entry_extracts_kh_ph_and_alert():
    """History entry: kh(i32) + ph(i32) + year(u16) + month(u8) +
    day(u8) + hour(u8) + minute(u8) + type_code(u8) + alert(u8)."""
    history = (
        _f(8.85)               # kh
        + _f(8.21)             # ph
        + struct.pack(">H", 2026)  # year
        + bytes([5, 7, 10, 30])    # month, day, hour, minute
        + bytes([1, 0])            # type_code, alert (0 = OK per ALERT_TEXT)
    )
    payload = (
        _f(7.5) + _f(8.9)          # alarms
        + bytes([0, 0, 0])         # state, percent, interval
        + _f(120.0) + bytes([0, 1])  # reagent_ml, reagent_low, history_count=1
        + history
    )
    out = parse_settings(payload)
    assert len(out["history"]) == 1
    h = out["history"][0]
    assert h["kh"] == 8.85
    assert h["ph"] == 8.21
    assert "2026-05-07T10:30" in h["timestamp"]
    assert h["alert_code"] == 0
    # The "summary" fields mirror history[0]:
    assert out["kh"] == 8.85
    assert out["ph"] == 8.21


def test_parse_handles_truncated_optional_tail():
    """A minimal payload (no calibration block, no next_test block, no
    adjustment block, no water counters) parses cleanly with all the
    optional fields set to None — never raises."""
    payload = _build_minimal_settings()
    out = parse_settings(payload)
    assert out["calibration_due"] is None
    assert out["calibration_warning"] is None
    assert out["next_test_time"] is None
    # Adjustment block fields are missing from the dict entirely
    # (not set to None) when truncated — parser silently skips. That's
    # the documented behaviour. Document it here so future "make these
    # consistent" refactors don't break the no-clobber logic that
    # depends on .get() returning None.
    assert "adjustment" not in out
