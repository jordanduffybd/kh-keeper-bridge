"""Tests for calibration-event detection and attribution.

The bridge surfaces a discrete `on_calibration_event` whenever the device's
`adjustment` field changes between settings frames. Attribution comes from
`_pending_calibration_attribution`, which is stashed by `calibrate_from_drop_test`
or `set_adjustment` before the command is queued, and consumed when the
device echoes the new value back. If the device returns something different
from what we asked for, the link is broken and we treat the change as
device-side.

These tests drive `_dispatch_frame` directly with hand-built settings frames,
so they exercise the same code path a live websocket frame would.
"""
from __future__ import annotations

import struct

import pytest

from kh_keeper_bridge.kh_keeper_bridge import KHKeeperClient, SCALE, encode_frame


def _settings_frame(*, alarm_low=7.5, alarm_high=8.8, state=0, percent=0,
                    interval=0, reagent_ml=120.0, reagent_low=False,
                    history_count=0, adjustment=0.0) -> bytes:
    """Build a settings payload that includes the adjustment field.

    The adjustment block sits after the calibration date + next-test-time
    + 'mixer/light/water_return' block at parser offset 25-37. To keep
    this simple, we use the truncated form that supplies adjustment via
    the same block the live parser reads — the prefix bytes are sized to
    match what parse_settings consumes prior to the adjustment field.
    """
    def f(v: float) -> bytes:
        return struct.pack(">i", int(round(v * SCALE)))

    return (
        f(alarm_low) + f(alarm_high)
        + bytes([state, percent, interval])
        + f(reagent_ml) + bytes([1 if reagent_low else 0, history_count])
        # Calibration date block: day(1) + month(1) + year(2) + warning(1) = 5
        + bytes([0, 0, 0, 0, 0])
        # Next-test block: year(2) + month(1) + day(1) + hour(1) + minute(1) = 6
        + bytes([0, 0, 0, 0, 0, 0])
        # adjustment(4) + remeasure_threshold(4) + water_return(1)
        # + used_water_ml_v0(4) + light(1) + mixer(1)
        + f(adjustment) + f(0.2) + bytes([0]) + f(0.0) + bytes([0, 0])
    )


def _dispatch_settings(client: KHKeeperClient, **kwargs):
    """Inject a settings frame through _handle_frame, the same path the
    websocket reader uses. Returns the awaitable so tests can await it.
    `ws=None` is safe — _handle_frame only touches ws for the refresh/config
    branch, which we never hit here."""
    payload = _settings_frame(**kwargs)
    frame = encode_frame("test-serial", "khRefresh", "settings", "txid", payload)
    return client._handle_frame(None, frame)


@pytest.fixture
def client():
    events: list[dict] = []

    async def capture(event):
        events.append(event)

    c = KHKeeperClient(host="test", on_state=lambda *a, **k: _async_noop())
    c.serial = "test-serial"
    c.on_calibration_event = capture
    c._captured_events = events
    return c


async def _async_noop(*_a, **_kw):
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
async def test_no_event_when_adjustment_unchanged(client):
    """Two frames with the same adjustment value must not fire the event."""
    await _dispatch_settings(client, adjustment=-1.0)
    await _dispatch_settings(client, adjustment=-1.0)
    assert client._captured_events == []


async def test_event_when_adjustment_changes(client):
    """Adjustment changing -1.0 → -0.5 must fire one event with the delta."""
    await _dispatch_settings(client, adjustment=-1.0)
    await _dispatch_settings(client, adjustment=-0.5)

    assert len(client._captured_events) == 1
    e = client._captured_events[0]
    assert e["prev"] == -1.0
    assert e["new"] == -0.5
    assert e["delta"] == 0.5
    assert e["serial"] == "test-serial"
    assert "ts" in e


async def test_no_event_on_first_frame(client):
    """The very first frame establishes baseline — no prior value to
    diff against, so no event."""
    await _dispatch_settings(client, adjustment=-0.57)
    assert client._captured_events == []


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
async def test_device_source_when_no_pending_attribution(client):
    """Adjustment change with no pending HA command is attributed to the
    device (likely physical UI or Smart Reef app)."""
    await _dispatch_settings(client, adjustment=-1.0)
    await _dispatch_settings(client, adjustment=-0.5)

    e = client._captured_events[0]
    assert e["source"] == "device"
    assert e["hanna_value"] is None


async def test_ha_drop_test_attribution_carries_hanna_value(client):
    """After calibrate_from_drop_test, the next adjustment change frame
    must be tagged with the Hanna value the user entered."""
    # Bootstrap state needed by calibrate_from_drop_test.
    client.last_state["kh"] = 9.85
    client.last_state["adjustment"] = -1.00
    # Trigger drop-test → queues khSet/adjust + stashes attribution.
    await client.calibrate_from_drop_test(8.20)
    # First settings frame after: baseline.
    await _dispatch_settings(client, adjustment=-1.00)
    assert client._captured_events == []
    # Now device echoes the new value back.
    # raw = 9.85 - (-1.00) = 10.85; new_adj = 8.20 - 10.85 = -2.65.
    await _dispatch_settings(client, adjustment=-2.65)

    assert len(client._captured_events) == 1
    e = client._captured_events[0]
    assert e["source"] == "ha_drop_test"
    assert e["hanna_value"] == 8.20
    assert e["new"] == -2.65


async def test_ha_raw_attribution_when_set_adjustment_used_directly(client):
    """set_adjustment without a Hanna value gets `ha_raw` attribution
    and no hanna_value."""
    await client.set_adjustment(-0.5)
    await _dispatch_settings(client, adjustment=-1.0)  # baseline
    await _dispatch_settings(client, adjustment=-0.5)  # echo

    e = client._captured_events[0]
    assert e["source"] == "ha_raw"
    assert e["hanna_value"] is None


async def test_attribution_discarded_when_device_returns_different_value(client):
    """If we asked for -0.5 but the device echoed -1.5 (e.g. the user
    overrode us at the physical UI mid-flight), don't tag it ha_*."""
    await client.set_adjustment(-0.5)
    await _dispatch_settings(client, adjustment=0.0)  # baseline
    # Device returns something far from what we asked for.
    await _dispatch_settings(client, adjustment=-1.5)

    e = client._captured_events[0]
    assert e["source"] == "device"
    assert e["hanna_value"] is None


async def test_pending_attribution_cleared_after_first_change(client):
    """A single calibrate_from_drop_test must only tag ONE subsequent
    change. If the user then twiddles the offset on the device, that
    second change is `device`, not `ha_drop_test`."""
    client.last_state["kh"] = 9.0
    client.last_state["adjustment"] = 0.0
    await client.calibrate_from_drop_test(8.50)
    # baseline + echo of HA command + later device twiddle.
    await _dispatch_settings(client, adjustment=0.0)
    await _dispatch_settings(client, adjustment=-0.50)
    await _dispatch_settings(client, adjustment=-0.30)

    assert [e["source"] for e in client._captured_events] == [
        "ha_drop_test", "device",
    ]
    assert client._captured_events[0]["hanna_value"] == 8.50
    assert client._captured_events[1]["hanna_value"] is None


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------
async def test_last_calibration_recorded_on_state(client):
    """The event is also stamped onto last_state as `last_calibration`
    so non-MQTT consumers (print_handler, tests) can see it."""
    await _dispatch_settings(client, adjustment=-1.0)
    await _dispatch_settings(client, adjustment=-0.5)
    assert client.last_state["last_calibration"]["new"] == -0.5
    assert client.last_state["last_calibration"]["delta"] == 0.5
