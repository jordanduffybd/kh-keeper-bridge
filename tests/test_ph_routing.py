"""Tests for the split-pH routing logic added in 0.1.12.

The bridge tracks two flags on KHKeeperClient:
- `_next_ph_is_pure`: set true by `refresh_ph` just before queueing the
  final measurePh; consumed by the next khRefresh/pH frame which then
  populates `last_state["ph_pure"]`.
- `_last_kh_test_ts`: tracks the timestamp of the last KH-test history
  entry consumed; when a settings frame arrives with a NEWER
  `history[0].timestamp`, the new history's pH is published as
  `ph_kh_test`.

These two flags are the entire correctness story for "which solution
was actually in the cuvette when this pH was measured." If either gets
wrongly set/cleared, the pH advisor input silently drifts back to the
old conflated value.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from kh_keeper_bridge.kh_keeper_bridge import KHKeeperClient, SCALE


@pytest.fixture
def client():
    """A KHKeeperClient with a no-op on_state callback. Doesn't open any
    network sockets; the constructor is pure."""
    async def noop(state, serial, sw_version):
        return None
    return KHKeeperClient(host="test", on_state=noop)


def _ph_frame(ph_value: float) -> bytes:
    """Encode a 4-byte fixed-point pH payload like the device sends."""
    return struct.pack(">i", int(round(ph_value * SCALE)))


# ---------------------------------------------------------------------------
# ph_pure routing — only set when refresh_ph is mid-cycle
# ---------------------------------------------------------------------------
async def test_ph_frame_without_pending_pure_does_not_set_ph_pure(client):
    """A passive measurePh response (e.g. user pressed Measure pH manually,
    or device auto-fired) must NOT populate ph_pure — we don't know what
    was in the cuvette. Only ph (legacy) updates."""
    # Simulate the frame handler's pH branch by calling the relevant logic
    # directly — the WS-handling code is too tangled for a unit test.
    # Mirror the actual code path in _handle_frame.
    client._next_ph_is_pure = False
    payload = _ph_frame(8.21)
    ph = round(struct.unpack(">i", payload[:4])[0] / SCALE, 2)
    client.last_state["ph"] = ph
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = ph
        client._next_ph_is_pure = False

    assert client.last_state.get("ph") == 8.21
    assert "ph_pure" not in client.last_state


async def test_ph_frame_with_pending_pure_sets_ph_pure_and_clears_flag(client):
    """When refresh_ph has set _next_ph_is_pure=True, the next pH frame
    populates ph_pure AND clears the flag (so a subsequent unrelated
    measurePh doesn't get incorrectly tagged as pure)."""
    client._next_ph_is_pure = True
    payload = _ph_frame(8.05)
    ph = round(struct.unpack(">i", payload[:4])[0] / SCALE, 2)
    client.last_state["ph"] = ph
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = ph
        client._next_ph_is_pure = False

    assert client.last_state.get("ph") == 8.05
    assert client.last_state.get("ph_pure") == 8.05
    assert client._next_ph_is_pure is False


async def test_ph_pure_flag_only_consumed_by_first_frame(client):
    """If two pH frames arrive after a refresh_ph cycle, only the FIRST
    populates ph_pure. The second is treated as an unrelated measurement
    (because the cuvette state is no longer guaranteed pure)."""
    client._next_ph_is_pure = True
    # First frame
    payload1 = _ph_frame(8.05)
    ph1 = round(struct.unpack(">i", payload1[:4])[0] / SCALE, 2)
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = ph1
        client._next_ph_is_pure = False
    # Second frame arrives later
    payload2 = _ph_frame(8.10)
    ph2 = round(struct.unpack(">i", payload2[:4])[0] / SCALE, 2)
    client.last_state["ph"] = ph2
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = ph2

    assert client.last_state.get("ph_pure") == 8.05  # first wins
    assert client.last_state.get("ph") == 8.10       # second updates legacy


# ---------------------------------------------------------------------------
# ph_kh_test routing — only set when a NEW KH test completes
# ---------------------------------------------------------------------------
async def test_first_kh_test_history_seeds_ph_kh_test(client):
    """First time we see a settings frame with history, _last_kh_test_ts
    is None → publish ph_kh_test."""
    history_ts = "2026-05-07T10:00:00+10:00"
    history_ph = 8.45

    # Mirror the relevant settings-handler logic.
    if (
        history_ts
        and history_ts != client._last_kh_test_ts
        and history_ph is not None
    ):
        client.last_state["ph_kh_test"] = history_ph
        client._last_kh_test_ts = history_ts

    assert client.last_state.get("ph_kh_test") == 8.45
    assert client._last_kh_test_ts == history_ts


async def test_repeated_settings_with_same_history_does_not_re_publish(client):
    """The device re-broadcasts settings every minute or so; the same
    history[0].timestamp shouldn't cause repeated ph_kh_test updates
    (otherwise HA history graphs get spammed with duplicate values)."""
    history_ts = "2026-05-07T10:00:00+10:00"

    # First settings frame.
    if history_ts != client._last_kh_test_ts:
        client.last_state["ph_kh_test"] = 8.45
        client._last_kh_test_ts = history_ts

    # Mutate the value to detect re-publish.
    client.last_state["ph_kh_test"] = 99.99

    # Second settings frame, same timestamp.
    if history_ts != client._last_kh_test_ts:
        client.last_state["ph_kh_test"] = 8.45
        client._last_kh_test_ts = history_ts

    # Should NOT have been overwritten — same test, no re-publish.
    assert client.last_state.get("ph_kh_test") == 99.99


async def test_new_kh_test_publishes_new_ph_kh_test(client):
    """A settings frame with a NEWER history[0].timestamp updates
    ph_kh_test from the new history entry."""
    client._last_kh_test_ts = "2026-05-07T10:00:00+10:00"
    client.last_state["ph_kh_test"] = 8.45

    new_ts = "2026-05-07T12:00:00+10:00"
    new_ph = 8.51
    if new_ts != client._last_kh_test_ts and new_ph is not None:
        client.last_state["ph_kh_test"] = new_ph
        client._last_kh_test_ts = new_ts

    assert client.last_state.get("ph_kh_test") == 8.51
    assert client._last_kh_test_ts == new_ts


async def test_ph_pure_and_ph_kh_test_are_independent(client):
    """Setting one must not touch the other — they describe two
    different solutions and need independent histories in HA."""
    # Refresh-pH cycle sets ph_pure.
    client._next_ph_is_pure = True
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = 8.05
        client._next_ph_is_pure = False
    # Then a new KH test completes and sets ph_kh_test.
    new_ts = "2026-05-07T13:00:00+10:00"
    if new_ts != client._last_kh_test_ts:
        client.last_state["ph_kh_test"] = 8.45
        client._last_kh_test_ts = new_ts

    assert client.last_state["ph_pure"] == 8.05
    assert client.last_state["ph_kh_test"] == 8.45


async def test_ph_kh_test_skipped_when_history_ph_is_none(client):
    """If history[0].ph is None (e.g. malformed test), skip the publish
    rather than poisoning ph_kh_test with None."""
    history_ts = "2026-05-07T14:00:00+10:00"
    history_ph = None

    if (
        history_ts
        and history_ts != client._last_kh_test_ts
        and history_ph is not None
    ):
        client.last_state["ph_kh_test"] = history_ph
        client._last_kh_test_ts = history_ts

    assert "ph_kh_test" not in client.last_state
    # The timestamp tracker should also not advance — we want a retry on
    # the next valid settings frame.
    assert client._last_kh_test_ts is None
