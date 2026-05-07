"""Tests for the refresh_ph progress phase tracking added in 0.1.13.

Each phase of `refresh_ph` writes `last_state["refresh_ph_phase"]` (and
an `eta` timestamp where applicable) and pushes the state via on_state
so the user sees progress in HA during the otherwise silent ~10-minute
window. The phase MUST reset to `idle` when the pure-water pH frame is
consumed; otherwise the dashboard tile would show "measuring" forever
after a successful cycle.
"""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime

import pytest

from kh_keeper_bridge.kh_keeper_bridge import KHKeeperClient, SCALE


@pytest.fixture
def client_with_recorder():
    """Returns (client, captured_states) where captured_states is a list
    of state-dict snapshots taken every time on_state is called."""
    captured: list[dict] = []

    async def recorder(state, serial, sw_version):
        captured.append(dict(state))

    c = KHKeeperClient(host="test", on_state=recorder)
    # Set serial so _set_refresh_ph_phase actually pushes (it skips
    # publication when self.serial is None).
    c.serial = "TESTSERIAL"
    return c, captured


# ---------------------------------------------------------------------------
# Phase transitions during refresh_ph
# ---------------------------------------------------------------------------
async def test_refresh_ph_emits_each_phase_in_order(client_with_recorder, monkeypatch):
    """The cycle must announce each phase via on_state in order:
    draining → filling → measuring. Each transition publishes a fresh
    state to MQTT so HA sees the progress."""
    client, captured = client_with_recorder
    client.last_state["state"] = "Idle"

    async def fake_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.refresh_ph(fill_ml=50.0)

    phases = [s.get("refresh_ph_phase") for s in captured if "refresh_ph_phase" in s]
    assert phases == [
        client.PHASE_DRAINING,
        client.PHASE_FILLING,
        client.PHASE_MEASURING,
    ]


async def test_refresh_ph_phase_eta_is_set_for_drain_and_fill(
    client_with_recorder, monkeypatch,
):
    """draining + filling have known durations → eta is populated.
    measuring has no defined duration (device responds in ~1s) → eta is None."""
    client, captured = client_with_recorder
    client.last_state["state"] = "Idle"

    async def fake_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.refresh_ph(fill_ml=50.0)

    by_phase = {s["refresh_ph_phase"]: s for s in captured if "refresh_ph_phase" in s}
    assert by_phase[client.PHASE_DRAINING]["refresh_ph_phase_eta"] is not None
    assert by_phase[client.PHASE_FILLING]["refresh_ph_phase_eta"] is not None
    assert by_phase[client.PHASE_MEASURING]["refresh_ph_phase_eta"] is None


async def test_refresh_ph_phase_eta_is_iso_8601_with_tz(
    client_with_recorder, monkeypatch,
):
    """The eta timestamp must be ISO-8601 with a timezone (HA's
    TIMESTAMP device class rejects naive datetimes — they show as
    'Unknown' in the UI). `datetime.fromisoformat` must round-trip it."""
    client, captured = client_with_recorder
    client.last_state["state"] = "Idle"

    async def fake_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.refresh_ph(fill_ml=50.0)

    drain_eta = next(
        s for s in captured if s.get("refresh_ph_phase") == client.PHASE_DRAINING
    )["refresh_ph_phase_eta"]
    parsed = datetime.fromisoformat(drain_eta)
    assert parsed.tzinfo is not None, "eta must be timezone-aware"


# ---------------------------------------------------------------------------
# Phase reset on cycle completion
# ---------------------------------------------------------------------------
async def test_pure_ph_frame_resets_phase_to_idle(client_with_recorder):
    """When the cuvette pH frame arrives after refresh_ph queued
    measurePh (i.e. _next_ph_is_pure is True), the phase MUST reset
    to idle so the dashboard tile clears. Without this reset, the
    dashboard would show 'measuring' indefinitely after every cycle."""
    client, _ = client_with_recorder
    # Simulate end of refresh_ph (just before measurePh response).
    client._next_ph_is_pure = True
    client.last_state["refresh_ph_phase"] = client.PHASE_MEASURING
    client.last_state["refresh_ph_phase_eta"] = None

    # Simulate the pH frame handler's relevant block.
    payload = struct.pack(">i", int(round(8.05 * SCALE)))
    ph = round(struct.unpack(">i", payload[:4])[0] / SCALE, 2)
    client.last_state["ph"] = ph
    if client._next_ph_is_pure:
        client.last_state["ph_pure"] = ph
        client._next_ph_is_pure = False
        client.last_state["refresh_ph_phase"] = client.PHASE_IDLE
        client.last_state["refresh_ph_phase_eta"] = None

    assert client.last_state["refresh_ph_phase"] == client.PHASE_IDLE
    assert client.last_state["refresh_ph_phase_eta"] is None
    assert client.last_state["ph_pure"] == 8.05


async def test_unrelated_ph_frame_does_not_touch_phase(client_with_recorder):
    """A live pH frame that's NOT from a refresh_ph cycle (e.g. the
    user pressed Measure pH manually) must leave the phase untouched.
    Otherwise pressing Measure pH would clear an in-progress refresh_ph
    phase indicator."""
    client, _ = client_with_recorder
    client._next_ph_is_pure = False
    client.last_state["refresh_ph_phase"] = client.PHASE_DRAINING
    client.last_state["refresh_ph_phase_eta"] = "2026-05-07T10:05:00+10:00"

    # Unrelated frame: just updates legacy ph, doesn't touch phase.
    payload = struct.pack(">i", int(round(8.05 * SCALE)))
    ph = round(struct.unpack(">i", payload[:4])[0] / SCALE, 2)
    client.last_state["ph"] = ph
    if client._next_ph_is_pure:
        client.last_state["refresh_ph_phase"] = client.PHASE_IDLE

    assert client.last_state["refresh_ph_phase"] == client.PHASE_DRAINING
    assert client.last_state["refresh_ph_phase_eta"] == "2026-05-07T10:05:00+10:00"


async def test_set_refresh_ph_phase_publishes_immediately(client_with_recorder):
    """Phase changes must push to MQTT right away — not wait for the
    next settings frame — so the user sees the progress live."""
    client, captured = client_with_recorder
    captured.clear()

    await client._set_refresh_ph_phase(client.PHASE_DRAINING, duration_s=300)
    assert len(captured) == 1
    assert captured[0]["refresh_ph_phase"] == client.PHASE_DRAINING


async def test_set_refresh_ph_phase_skips_publish_when_no_serial():
    """Before the device's first config frame arrives, self.serial is
    None and on_state would crash if called with it. The phase setter
    must skip publication in that case (the state still updates in
    last_state for later broadcast)."""
    captured: list[dict] = []

    async def recorder(state, serial, sw_version):
        captured.append(dict(state))

    c = KHKeeperClient(host="test", on_state=recorder)
    assert c.serial is None  # not joined yet

    await c._set_refresh_ph_phase(c.PHASE_DRAINING, duration_s=300)

    assert captured == []  # no publish
    assert c.last_state["refresh_ph_phase"] == c.PHASE_DRAINING  # state updated
