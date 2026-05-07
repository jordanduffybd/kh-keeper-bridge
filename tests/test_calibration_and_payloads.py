"""Tests for calibration math + outbound payload encoding.

`calibrate_from_drop_test` is the function the user invokes after
running a Hanna drop test against the KH Keeper. It computes the
adjustment delta and queues a `khSet/adjust` command. If the math
drifts, every drop-test calibration will move the displayed KH in the
WRONG direction — silently, since the device just trusts the value.

Outbound command payloads (`set_alarms`, `set_adjustment`,
`set_interval`) are pure byte builders — round-trip them against the
device JS-derived format to catch encoding regressions.
"""
from __future__ import annotations

import struct

import pytest

from kh_keeper_bridge.kh_keeper_bridge import KHKeeperClient, SCALE


@pytest.fixture
def client():
    async def noop(*a, **kw):
        return None
    return KHKeeperClient(host="test", on_state=noop)


# ---------------------------------------------------------------------------
# Drop-test calibration math
# ---------------------------------------------------------------------------
async def test_calibrate_from_drop_test_computes_correct_adjustment(client):
    """Device displays 9.85 with current adjustment -1.00. Drop test
    measures 8.20.
    raw = displayed - current_adj = 9.85 - (-1.00) = 10.85
    new_adj = drop_test - raw = 8.20 - 10.85 = -2.65
    """
    client.last_state["kh"] = 9.85
    client.last_state["adjustment"] = -1.00

    await client.calibrate_from_drop_test(8.20)

    # The most recently queued command is the new adjustment.
    cmd, sub, payload = client.commands.get_nowait()
    assert (cmd, sub) == ("khSet", "adjust")
    raw = struct.unpack(">i", payload)[0]
    assert raw == int(round(-2.65 * SCALE))


async def test_calibrate_from_drop_test_with_zero_initial_adjustment(client):
    """Fresh device with adjustment=0. Displayed=8.50, drop_test=8.00 →
    new_adj = 8.00 - (8.50 - 0) = -0.50."""
    client.last_state["kh"] = 8.50
    client.last_state["adjustment"] = 0.0

    await client.calibrate_from_drop_test(8.00)
    _, _, payload = client.commands.get_nowait()
    raw = struct.unpack(">i", payload)[0]
    assert raw == int(round(-0.50 * SCALE))


async def test_calibrate_from_drop_test_rejects_out_of_range(client):
    """Any drop-test value outside 5-15 dKH is rejected — sanity check
    against typo (e.g. 82 instead of 8.2)."""
    client.last_state["kh"] = 8.50
    client.last_state["adjustment"] = 0.0

    with pytest.raises(ValueError, match="5-15"):
        await client.calibrate_from_drop_test(82.0)
    with pytest.raises(ValueError, match="5-15"):
        await client.calibrate_from_drop_test(2.0)
    # No command queued.
    assert client.commands.empty()


async def test_calibrate_from_drop_test_requires_loaded_state(client):
    """If we haven't received a settings frame yet, kh and adjustment
    are absent → calibration must refuse, not crash."""
    with pytest.raises(RuntimeError, match="No current measurement"):
        await client.calibrate_from_drop_test(8.20)


# ---------------------------------------------------------------------------
# Outbound payload encoders
# ---------------------------------------------------------------------------
async def test_set_alarms_payload_format(client):
    """khSet/settings payload for alarms: 4 bytes low + 4 bytes high
    (both fixed-point) + 9 zero bytes. Replicates device JS exactly."""
    await client.set_alarms(low_dkh=7.5, high_dkh=8.8)
    cmd, sub, payload = client.commands.get_nowait()
    assert (cmd, sub) == ("khSet", "settings")
    assert len(payload) == 17
    low = struct.unpack(">i", payload[0:4])[0]
    high = struct.unpack(">i", payload[4:8])[0]
    assert low == int(round(7.5 * SCALE))
    assert high == int(round(8.8 * SCALE))
    assert payload[8:] == b"\x00" * 9


async def test_set_alarms_rejects_inverted_range(client):
    """Low > High would brick the alarm UI — refuse before sending."""
    with pytest.raises(ValueError, match="alarm low must be <= high"):
        await client.set_alarms(low_dkh=9.0, high_dkh=8.0)
    assert client.commands.empty()


async def test_set_adjustment_signed_fixed_point(client):
    """Negative adjustment must round-trip through signed i32 fixed-point."""
    await client.set_adjustment(-1.50)
    _, _, payload = client.commands.get_nowait()
    raw = struct.unpack(">i", payload)[0]
    assert raw == int(round(-1.50 * SCALE))


async def test_set_interval_valid_codes(client):
    """Codes 0..6 accepted; code 7 (Custom) rejected per docstring."""
    await client.set_interval(2)  # 4h
    _, sub, payload = client.commands.get_nowait()
    assert sub == "setInterval"
    assert payload == bytes([2, 0, 0, 0])


async def test_set_interval_rejects_invalid_codes(client):
    with pytest.raises(ValueError, match="0-6"):
        await client.set_interval(7)
    with pytest.raises(ValueError, match="0-6"):
        await client.set_interval(-1)
    assert client.commands.empty()


# ---------------------------------------------------------------------------
# Idle gate — refuses commands during measurement
# ---------------------------------------------------------------------------
async def test_require_idle_allows_when_state_idle(client):
    client.last_state["state"] = "Idle"
    assert client._require_idle("test action") is True


async def test_require_idle_allows_when_state_unknown(client):
    """Empty state (no settings frame yet) is treated as allow — the
    user might be issuing a startup command before any frame arrives."""
    assert client._require_idle("test action") is True


async def test_require_idle_refuses_when_measuring(client):
    """During a real KH measurement, sending any command has been
    observed to abort the test. _require_idle gates this."""
    client.last_state["state"] = "Measuring"
    assert client._require_idle("test action") is False


async def test_require_idle_refuses_when_dosing(client):
    client.last_state["state"] = "Dosing reagent"
    assert client._require_idle("test action") is False
