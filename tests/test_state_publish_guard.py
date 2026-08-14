"""Tests for the state-topic publish guard added in 0.1.16.

Production symptom (HA Core 2026.7.2): 753 occurrences of

    WARNING homeassistant.components.mqtt.sensor
    Invalid state message '' from 'kh_keeper/RFKH022304210017/state'

HA's MQTT sensor logs that warning from the `date`/`timestamp` device-class
branch, and the '' it prints is the *rendered value_template*, not the raw
MQTT payload. `{{ value_json.refresh_ph_phase_eta }}` renders to an empty
string whenever that key is missing from the published JSON (Jinja
undefined), which was every single publish until a refresh_ph cycle ran.

So `MQTTPublisher._publish_state` now:
  - refuses to publish an empty/whitespace-only payload to the state topic
    (skipping entirely, logged at DEBUG so the noise isn't just relocated),
  - always includes the timestamp/date-typed keys, as JSON null when
    unknown, so their templates render "None" (which HA maps to `unknown`
    without a warning) instead of "".

Discovery/config topics are untouched — clearing a retained discovery config
with an empty payload is a legitimate MQTT pattern.
"""
from __future__ import annotations

import json

import pytest

from kh_keeper_bridge.kh_keeper_bridge import MQTTPublisher

SERIAL = "RFKH022304210017"


class _FakeMQTTClient:
    """Records publishes instead of talking to a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object, bool]] = []

    def publish(self, topic, payload=None, retain=False, **_kw):
        self.published.append((topic, payload, retain))

    def subscribe(self, _topic):
        return None


@pytest.fixture
def publisher():
    """An MQTTPublisher with a fake broker client.

    Built with `__new__` so we skip `__init__`, which imports paho and opens
    a real socket. `discovered=True` skips the discovery pass so the only
    publishes we see come from the state path under test.
    """
    pub = MQTTPublisher.__new__(MQTTPublisher)
    pub.client = _FakeMQTTClient()
    pub.discovery_prefix = "homeassistant"
    pub.node_prefix = "kh_keeper"
    pub.client_ref = None
    pub.loop = None
    pub.discovered = True
    pub.command_handlers = {}
    return pub


def _state_payloads(pub) -> list[str]:
    topic = pub._state_topic(SERIAL)
    return [p for t, p, _r in pub.client.published if t == topic]


# ---------------------------------------------------------------------------
# (a) empty / None state is never published
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", [{}, None])
async def test_empty_state_is_not_published(publisher, state):
    """Nothing at all should hit the broker when there's no state — not an
    empty string, not `{}`. The bridge just skips the publish."""
    await publisher(state, SERIAL, "1.0.0")

    assert publisher.client.published == []


def test_publish_state_returns_false_for_empty_state(publisher):
    """The helper reports that it skipped, so the caller can suppress its
    'Published: ...' log line too."""
    assert publisher._publish_state(SERIAL, {}) is False
    assert publisher._publish_state(SERIAL, None) is False
    assert publisher.client.published == []


async def test_no_empty_or_whitespace_payload_ever_reaches_state_topic(publisher):
    """Belt-and-braces: whatever we feed it, the state topic must never see
    an empty or whitespace-only payload."""
    for state in ({}, None, {"kh": 8.1}, {"state": "Idle"}):
        await publisher(state, SERIAL, "1.0.0")

    payloads = _state_payloads(publisher)
    assert payloads  # the populated ones did publish
    assert all(p.strip() for p in payloads)


# ---------------------------------------------------------------------------
# (b) a normal populated state IS published, values unchanged
# ---------------------------------------------------------------------------
async def test_populated_state_is_published_unchanged(publisher):
    """A real state snapshot publishes to the retained state topic with every
    value intact. `history` is still stripped (too big for HA), and the
    timestamp keys already present must not be overwritten."""
    state = {
        "kh": 8.12,
        "ph": 8.45,
        "state": "Idle",
        "state_percent": 0,
        "reagent_ml": 118.4,
        "last_test_time": "2026-08-14T04:00:00+10:00",
        "next_test_time": "2026-08-14T08:00:00+10:00",
        "calibration_due": "2026-11-02",
        "refresh_ph_phase": "idle",
        "refresh_ph_phase_eta": None,
        "history": [{"kh": 8.12, "ph": 8.45}],
    }

    await publisher(dict(state), SERIAL, "1.0.0")

    published = publisher.client.published
    assert len(published) == 1
    topic, payload, retain = published[0]
    assert topic == f"kh_keeper/{SERIAL}/state"
    assert retain is True

    decoded = json.loads(payload)
    assert "history" not in decoded
    for key, value in state.items():
        if key == "history":
            continue
        assert decoded[key] == value


async def test_missing_timestamp_keys_are_published_as_null(publisher):
    """Root-cause regression test. A partial state — e.g. a khRefresh/status
    or khRefresh/pH frame arriving before the first settings frame, or any
    publish before a refresh_ph cycle has ever run — must still carry the
    date/timestamp-typed keys as JSON null. If they're absent, HA renders
    the template as '' and logs `Invalid state message ''`."""
    await publisher({"state": "Measuring", "state_percent": 42}, SERIAL, "1.0.0")

    decoded = json.loads(_state_payloads(publisher)[0])
    for key in MQTTPublisher.TIMESTAMP_KEYS:
        assert key in decoded, f"{key} missing — HA will render '' and warn"
        assert decoded[key] is None
    # Real values still come through.
    assert decoded["state"] == "Measuring"
    assert decoded["state_percent"] == 42
