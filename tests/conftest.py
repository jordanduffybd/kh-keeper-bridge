"""Test fixtures for kh-keeper-bridge.

The bridge module imports `paho.mqtt.client` and `websockets` lazily, so
pure protocol/state-machine tests don't need either installed. Heavy
network code lives behind those imports and is exercised in integration
tests against a real device, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `kh_keeper_bridge.kh_keeper_bridge` importable regardless of how
# pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))
