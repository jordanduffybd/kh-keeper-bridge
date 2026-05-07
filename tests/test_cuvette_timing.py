"""Tests guarding the cuvette-cycle timing constants.

These are operational reality (per device observation, not protocol spec):
empty drains for ~5 minutes, fill takes ~5 minutes, device is silent and
ignores commands during each phase. If anyone shaves these back to "what
seems plenty" they'll silently break refresh_ph cycles like the 0.1.11-
and-earlier 55s sleep did.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from kh_keeper_bridge.kh_keeper_bridge import KHKeeperClient, SCALE


def test_empty_duration_is_at_least_5_minutes():
    """Device drains for ~5 minutes. Anything less and we issue the fill
    command into the silent window and it gets dropped/ignored."""
    assert KHKeeperClient.EMPTY_DURATION_S >= 300


def test_fill_duration_is_at_least_5_minutes():
    """Device fills for ~5 minutes. Anything less and measurePh fires
    against partially-filled cuvette → useless reading."""
    assert KHKeeperClient.FILL_DURATION_S >= 300


async def test_refresh_ph_sleeps_full_cycle_before_measurePh(monkeypatch):
    """End-to-end ordering: empty queued → sleep → fill queued → sleep →
    measurePh queued + _next_ph_is_pure flag set. Stub asyncio.sleep so
    the test runs in milliseconds.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def noop(*a, **kw):
        return None
    client = KHKeeperClient(host="test", on_state=noop)
    # _require_idle reads last_state["state"]; default no value → idle.
    client.last_state["state"] = "Idle"

    await client.refresh_ph(fill_ml=50.0)

    # Three commands queued in order: empty, doseAquarium, measurePh.
    queued: list[tuple[str, str, bytes]] = []
    while not client.commands.empty():
        queued.append(client.commands.get_nowait())
    cmds = [(c, sc) for c, sc, _ in queued]
    assert cmds == [
        ("khCommand", "empty"),
        ("khCommand", "doseAquarium"),
        ("khCommand", "measurePh"),
    ]
    # Two sleeps happened, both >= 5 minutes.
    assert len(sleeps) == 2
    assert all(s >= 300 for s in sleeps), (
        f"refresh_ph sleeps must each be >= 300s; got {sleeps}"
    )
    # The flag is armed for the next pH frame.
    assert client._next_ph_is_pure is True


async def test_refresh_ph_refuses_when_busy(monkeypatch):
    """If the device is mid-measurement, refresh_ph must NOT queue
    anything — sending pump commands during a real KH test has been
    observed to abort the test."""
    async def noop(*a, **kw):
        return None
    client = KHKeeperClient(host="test", on_state=noop)
    client.last_state["state"] = "Measuring"

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await client.refresh_ph(fill_ml=50.0)

    assert client.commands.empty(), "refresh_ph must not queue when busy"
    assert sleeps == [], "no sleeps if we never started the cycle"
    assert client._next_ph_is_pure is False
