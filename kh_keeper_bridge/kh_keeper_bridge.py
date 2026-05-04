#!/usr/bin/env python3
"""
Reef Factory KH Keeper -> Home Assistant bridge.

Connects to a Reef Factory KH Keeper's local WebSocket, decodes its binary
protocol, and either prints decoded values (test mode) or publishes them to
MQTT with Home Assistant auto-discovery (production mode).

Read sensors:
  KH, pH, status, measurement progress %, last/next test times, reagent mL,
  calibration due date, alarm low/high, current adjustment.

Device-write commands (also exposed as HA buttons / numbers / selects):
  - take a manual test
  - cancel an in-progress test
  - calibrate from drop test (you give your real KH, bridge computes adjustment)
  - set KH adjustment directly (signed dKH offset)
  - set KH alarm low/high thresholds
  - set measurement interval
  - run pump accuracy tests (aquarium pump 50 mL, reagent pump 5 mL)

Full pump CALIBRATION (multi-step "start → run → enter actual mL → device
computes ratio") is intentionally NOT exposed — it's awkward in HA. Use the
device's web UI or the Smart Reef app for that.

Usage:
    # Read-only test
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test

    # Trigger a manual KH test
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --trigger-test

    # Cancel an in-progress measurement
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --cancel-measurement

    # Calibrate from drop test -- you measured 8.20 with your test kit
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --calibrate-kh 8.20

    # Set KH adjustment directly (raw signed delta, less intuitive)
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --set-adjustment -1.5

    # Set alarm low / high
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --set-alarms 7.5 8.8

    # Set interval (codes: 0=1h, 1=2h, 2=4h, 3=8h, 4=12h, 5=Off, 6=6h)
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --set-interval 2

    # Pump accuracy tests
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --dose-aquarium-test
    python3 kh_keeper_bridge.py --host 192.168.1.229 --test --dose-reagent-test

    # Production: publish to MQTT (number/select/button entities for all of the above)
    python3 kh_keeper_bridge.py \\
        --host 192.168.1.229 \\
        --mqtt-host 192.168.1.10 \\
        --mqtt-user mqttuser --mqtt-pass mqttpass

Dependencies:
    pip install websockets paho-mqtt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import sys
import time
from datetime import datetime
from typing import Any

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("Missing dependency. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)

LOGGER = logging.getLogger("kh_keeper_bridge")

# Values are encoded as 32-bit signed ints in fixed-point with 4 decimals.
SCALE = 10000.0

# Measurement state codes (the first byte of khRefresh/status frames).
STATE_TEXT = {
    0: "Idle",
    1: "Measuring",
    2: "Calibrating",
    3: "Cancelling",
    4: "Remeasuring",
}

# Per-measurement alert codes (history[0].alert).
ALERT_TEXT = {
    0: "OK",
    2: "Too low",
    3: "Too high",
    4: "Below range",
    5: "Above range",
    7: "No reagent",
    8: "Probe error",
    9: "Rapid change",
}

# Measurement interval enum.
INTERVAL_TEXT = {
    0: "1 hour",
    1: "2 hours",
    2: "4 hours",
    3: "8 hours",
    4: "12 hours",
    5: "Off",
    6: "6 hours",
    7: "Custom",
}

# Mixer speed enum.
MIXER_TEXT = {0: "Slow", 1: "Medium", 2: "Fast"}


# ---------------------------------------------------------------------------
# Binary protocol helpers
# ---------------------------------------------------------------------------

def encode_frame(serial: str, command: str, subcommand: str, txid: str, payload: bytes = b"") -> bytes:
    """Encode a frame in the device's binary protocol.

    Wire format: serial\\0command\\0subcommand\\0txid\\0payload\\0
    """
    return (
        serial.encode("ascii") + b"\x00"
        + command.encode("ascii") + b"\x00"
        + subcommand.encode("ascii") + b"\x00"
        + txid.encode("ascii") + b"\x00"
        + payload + b"\x00"
    )


def decode_frame(data: bytes) -> tuple[str, str, str, str, bytes]:
    """Decode a frame: returns (serial, command, subcommand, txid, payload)."""
    parts: list[str] = []
    cursor = 0
    for _ in range(4):
        nul = data.index(0, cursor)
        parts.append(data[cursor:nul].decode("ascii", errors="replace"))
        cursor = nul + 1
    return parts[0], parts[1], parts[2], parts[3], data[cursor:]


class Reader:
    """Tiny helper to read primitives sequentially from a bytes buffer."""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.offset = 0

    def remaining(self) -> int:
        return len(self.buf) - self.offset

    def u8(self) -> int:
        v = self.buf[self.offset]
        self.offset += 1
        return v

    def u16(self) -> int:
        v = (self.buf[self.offset] << 8) | self.buf[self.offset + 1]
        self.offset += 2
        return v

    def i32(self) -> int:
        v = struct.unpack(">i", self.buf[self.offset:self.offset + 4])[0]
        self.offset += 4
        return v

    def fixed(self) -> float:
        return self.i32() / SCALE


# ---------------------------------------------------------------------------
# Settings frame parser
# ---------------------------------------------------------------------------

def parse_settings(payload: bytes) -> dict[str, Any]:
    """Decode a khRefresh/settings payload into a dict.

    Field order mirrors the device JS parser. Anything we can't read past the
    end is left as None.
    """
    r = Reader(payload)
    out: dict[str, Any] = {}

    out["alarm_kh_low"] = round(r.fixed(), 2)
    out["alarm_kh_high"] = round(r.fixed(), 2)

    state_code = r.u8()
    out["state_code"] = state_code
    out["state"] = STATE_TEXT.get(state_code, f"Unknown ({state_code})")
    out["state_percent"] = r.u8()

    interval_code = r.u8()
    out["interval_code"] = interval_code
    out["interval"] = INTERVAL_TEXT.get(interval_code, f"Unknown ({interval_code})")

    out["reagent_ml"] = round(r.fixed(), 2)
    out["reagent_low"] = bool(r.u8())

    history_count = r.u8()
    history: list[dict[str, Any]] = []
    for _ in range(history_count):
        kh_raw = r.i32()
        ph_raw = r.i32()
        year = r.u16()
        month = ((r.u8() - 1) % 12) + 1
        day = r.u8()
        hour = r.u8()
        minute = r.u8()
        type_code = r.u8()
        alert = r.u8()
        try:
            ts = datetime(year, month, day, hour, minute).isoformat() if year > 0 else None
        except ValueError:
            ts = None
        history.append({
            "kh": round(kh_raw / SCALE, 2),
            "ph": round(ph_raw / SCALE, 2),
            "timestamp": ts,
            "type_code": type_code,
            "alert_code": alert,
            "alert": ALERT_TEXT.get(alert, f"Unknown ({alert})"),
        })
    out["history"] = history

    if history:
        latest = history[0]
        out["kh"] = latest["kh"]
        out["ph"] = latest["ph"]
        out["last_test_time"] = latest["timestamp"]
        out["last_test_alert"] = latest["alert"]
        out["last_test_alert_code"] = latest["alert_code"]
    else:
        out["kh"] = None
        out["ph"] = None
        out["last_test_time"] = None
        out["last_test_alert"] = None
        out["last_test_alert_code"] = None

    try:
        cal_day = r.u8()
        cal_month = ((r.u8() - 1) % 12) + 1
        cal_year = r.u16()
        cal_warning = r.u8()
        out["calibration_due"] = (
            datetime(cal_year, cal_month, cal_day).date().isoformat()
            if cal_year > 0 else None
        )
        out["calibration_warning"] = bool(cal_warning)
    except (IndexError, ValueError, struct.error):
        out["calibration_due"] = None
        out["calibration_warning"] = None

    try:
        nxt_year = r.u16()
        nxt_month = ((r.u8() - 1) % 12) + 1
        nxt_day = r.u8()
        nxt_hour = r.u8()
        nxt_minute = r.u8()
        out["next_test_time"] = (
            datetime(nxt_year, nxt_month, nxt_day, nxt_hour, nxt_minute).isoformat()
            if nxt_year > 0 else None
        )
    except (IndexError, ValueError, struct.error):
        out["next_test_time"] = None

    try:
        out["adjustment"] = round(r.fixed(), 2)
        out["remeasure_threshold"] = round(r.fixed(), 2)
        out["water_return"] = bool(r.u8())
        out["used_water_ml_v0"] = round(r.fixed(), 2)
        out["light"] = bool(r.u8())
        mixer = r.u8()
        out["mixer_speed_code"] = mixer
        out["mixer_speed"] = MIXER_TEXT.get(mixer, f"Unknown ({mixer})")
    except (IndexError, struct.error):
        pass

    try:
        out["waste_current_ml"] = round(r.fixed(), 2)
        out["waste_limit_ml"] = round(r.fixed(), 2)
        out["used_water_ml"] = round(r.fixed(), 2)
    except (IndexError, struct.error):
        pass

    return out


def parse_status(payload: bytes) -> dict[str, Any]:
    """Decode the short khRefresh/status frame: 1 byte state + 1 byte percent."""
    if len(payload) < 2:
        return {}
    state_code = payload[0]
    return {
        "state_code": state_code,
        "state": STATE_TEXT.get(state_code, f"Unknown ({state_code})"),
        "state_percent": payload[1],
    }


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class KHKeeperClient:
    def __init__(self, host: str, on_state):
        self.host = host
        self.on_state = on_state
        self.serial: str | None = None
        self.sw_version: str | None = None
        self.last_state: dict[str, Any] = {}
        self.commands: asyncio.Queue[tuple[str, str, bytes]] = asyncio.Queue()
        self._connected = asyncio.Event()

    async def queue_command(self, command: str, subcommand: str, payload: bytes = b"") -> None:
        await self.commands.put((command, subcommand, payload))

    async def take_now(self) -> None:
        LOGGER.info("Queued: take KH test now")
        await self.queue_command("khMeasurement", "takeNow", b"")

    async def cancel_measurement(self) -> None:
        """Cancel an in-progress KH measurement (`khMeasurement/cancel`)."""
        LOGGER.info("Queued: cancel current measurement")
        await self.queue_command("khMeasurement", "cancel", b"")

    async def set_adjustment(self, dkh: float) -> None:
        """Set the absolute KH adjustment (signed dKH offset).

        E.g. -1.5 means the device subtracts 1.5 dKH from raw measurements
        before display. Pass 0 to reset adjustment.
        """
        raw = int(round(dkh * SCALE))
        payload = struct.pack(">i", raw)
        LOGGER.info("Queued: set adjustment to %+.2f dKH (raw=%d)", dkh, raw)
        await self.queue_command("khSet", "adjust", payload)

    async def calibrate_from_drop_test(self, drop_test_kh: float) -> None:
        """Take a drop-test KH value and compute/apply the adjustment so the
        device's displayed KH will match it.

        Math:
            displayed = raw + current_adjustment
            new_adjustment = drop_test - raw
                           = drop_test - (displayed - current_adjustment)
        """
        if not 5.0 <= drop_test_kh <= 15.0:
            raise ValueError(f"drop_test_kh must be 5-15 dKH, got {drop_test_kh}")

        displayed = self.last_state.get("kh")
        current_adj = self.last_state.get("adjustment")
        if displayed is None or current_adj is None:
            raise RuntimeError(
                "No current measurement yet — wait until the bridge has received "
                "a settings frame (state should show kh and adjustment)."
            )

        raw_kh = displayed - current_adj
        new_adj = round(drop_test_kh - raw_kh, 2)
        LOGGER.info(
            "Drop-test calibration: displayed=%.2f current_adj=%+.2f → raw=%.2f "
            "drop_test=%.2f → new_adjustment=%+.2f",
            displayed, current_adj, raw_kh, drop_test_kh, new_adj,
        )
        await self.set_adjustment(new_adj)

    async def set_alarms(self, low_dkh: float, high_dkh: float) -> None:
        """Set KH alarm low/high thresholds.

        Replicates the device JS: 17-byte payload with 4 bytes low + 4 bytes
        high in fixed-point and the rest zeros.
        """
        if low_dkh > high_dkh:
            raise ValueError("alarm low must be <= high")
        low_raw = int(round(low_dkh * SCALE))
        high_raw = int(round(high_dkh * SCALE))
        payload = struct.pack(">ii", low_raw, high_raw) + b"\x00" * 9
        LOGGER.info("Queued: alarms low=%.2f high=%.2f dKH", low_dkh, high_dkh)
        await self.queue_command("khSet", "settings", payload)

    async def set_interval(self, code: int) -> None:
        """Set measurement interval. Codes: 0=1h, 1=2h, 2=4h, 3=8h, 4=12h,
        5=Off, 6=6h. (7=Custom not supported here.)"""
        if not 0 <= code <= 6:
            raise ValueError("interval code must be 0-6")
        payload = bytes([code, 0, 0, 0])
        LOGGER.info("Queued: interval code=%d (%s)", code, INTERVAL_TEXT.get(code))
        await self.queue_command("khMeasurement", "setInterval", payload)

    async def dose_aquarium_test(self, ml: float = 50.0) -> None:
        """Run the aquarium pump for the given volume (default 50 mL).

        Useful for accuracy verification: catch the output and confirm it
        matches. Stock dose is 50 mL (range 49.5-50.5 expected).
        """
        raw = int(round(ml * SCALE))
        payload = struct.pack(">i", raw)
        LOGGER.info("Queued: dose aquarium pump %.2f mL", ml)
        await self.queue_command("khCommand", "doseAquarium", payload)

    async def dose_reagent_test(self, ml: float = 5.0) -> None:
        """Run the reagent pump A for the given volume (default 5 mL)."""
        raw = int(round(ml * SCALE))
        payload = struct.pack(">i", raw)
        LOGGER.info("Queued: dose reagent pump %.2f mL", ml)
        await self.queue_command("khCommand", "doseA", payload)

    async def wait_until_ready(self) -> None:
        await self._connected.wait()

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            try:
                await self._run_once()
                backoff = 1
            except Exception as exc:  # noqa: BLE001
                self._connected.clear()
                LOGGER.warning("Connection failed: %s. Retrying in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _run_once(self) -> None:
        uri = f"ws://{self.host}/controler"
        origin_header = f"http://{self.host}"
        LOGGER.info("Connecting to %s", uri)
        async with websockets.connect(
            uri,
            subprotocols=["arduino"],
            ping_interval=None,
            origin=origin_header,
            user_agent_header="Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/605.1.15",
        ) as ws:
            LOGGER.info("Connected. Requesting config...")
            await ws.send(encode_frame("", "get", "config", "", b""))

            ping_task = asyncio.create_task(self._ping_loop(ws))
            cmd_task = asyncio.create_task(self._command_pump(ws))
            try:
                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    await self._handle_frame(ws, bytes(raw))
            finally:
                ping_task.cancel()
                cmd_task.cancel()

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                if self.serial:
                    await ws.send(encode_frame(self.serial, "ping", "ping", "", b""))
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    async def _command_pump(self, ws) -> None:
        """Forward queued commands to the device once we're joined."""
        try:
            await self._connected.wait()
            while True:
                command, subcommand, payload = await self.commands.get()
                if not self.serial:
                    LOGGER.warning("Drop command %s/%s — no serial", command, subcommand)
                    continue
                txid = f"cmd_{int(time.time() * 1000)}"
                LOGGER.info("→ device: %s/%s", command, subcommand)
                try:
                    await ws.send(encode_frame(self.serial, command, subcommand, txid, payload))
                except ConnectionClosed:
                    # Re-queue and let the reconnect loop handle it.
                    await self.commands.put((command, subcommand, payload))
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_frame(self, ws, data: bytes) -> None:
        try:
            serial, command, subcommand, txid, payload = decode_frame(data)
        except (ValueError, IndexError):
            LOGGER.debug("Malformed frame: %s", data.hex())
            return

        LOGGER.debug("RX %s/%s txid=%s len=%d", command, subcommand, txid, len(payload))

        if command == "refresh" and subcommand == "config":
            self._parse_config(payload)
            await self._join_kh(ws)
            return

        if command == "khRefresh":
            if subcommand == "settings":
                try:
                    update = parse_settings(payload)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Failed to parse settings: %s", exc)
                    return
                self.last_state.update(update)
                self._connected.set()
                await self.on_state(dict(self.last_state), self.serial, self.sw_version)
                return

            if subcommand == "status":
                update = parse_status(payload)
                if update:
                    self.last_state.update(update)
                    await self.on_state(dict(self.last_state), self.serial, self.sw_version)
                return

        # Other frames (calibration, alerts, pong, etc.) are ignored for now.

    def _parse_config(self, payload: bytes) -> None:
        nul = payload.index(0)
        self.serial = payload[:nul].decode("ascii", errors="replace")
        LOGGER.info("Device serial: %s", self.serial)

        # Layout from device JS: serial\0, langByte, buildTypeByte, 5 bytes sw_version, ...
        try:
            cursor = nul + 1
            cursor += 1  # language byte
            cursor += 1  # build-type byte
            sw = payload[cursor:cursor + 5].decode("ascii", errors="replace").rstrip("\x00 ")
            self.sw_version = sw or None
            LOGGER.info("Software version: %s", sw)
        except (IndexError, UnicodeDecodeError):
            self.sw_version = None

    async def _join_kh(self, ws) -> None:
        if not self.serial:
            return
        txid = f"join_{int(time.time() * 1000)}"
        payload = self.serial.encode("ascii") + b"\x00"
        LOGGER.info("Joining KH stream as %s", self.serial)
        await ws.send(encode_frame(self.serial, "khConnect", "join", txid, payload))


# ---------------------------------------------------------------------------
# Output backends
# ---------------------------------------------------------------------------

async def print_handler(state: dict, serial: str, sw_version: str | None) -> None:
    print()
    print(f"=== {datetime.now().isoformat(timespec='seconds')} | {serial} (fw {sw_version}) ===")
    summary_keys = [
        "kh", "ph", "state", "state_percent",
        "last_test_alert", "last_test_time", "next_test_time",
        "interval", "reagent_ml", "reagent_low",
        "calibration_due", "calibration_warning",
        "alarm_kh_low", "alarm_kh_high", "adjustment",
    ]
    for k in summary_keys:
        if k in state:
            print(f"  {k:>22}: {state[k]}")
    print(f"  {'history (count)':>22}: {len(state.get('history', []))}")
    if state.get("history"):
        print(f"  {'history[0]':>22}: {state['history'][0]}")


class MQTTPublisher:
    """Publishes decoded state to MQTT with HA auto-discovery, and exposes a
    'Take Now' button that forwards presses back to the device."""

    def __init__(self, host: str, port: int, user: str | None, password: str | None,
                 client_ref: KHKeeperClient,
                 loop: asyncio.AbstractEventLoop,
                 discovery_prefix: str = "homeassistant",
                 node_prefix: str = "kh_keeper"):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("Missing dependency. Install with: pip install paho-mqtt", file=sys.stderr)
            sys.exit(1)

        self.client = mqtt.Client(
            client_id=f"kh_keeper_bridge_{int(time.time())}",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if user:
            self.client.username_pw_set(user, password)
        self.client.on_message = self._on_message
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

        self.discovery_prefix = discovery_prefix
        self.node_prefix = node_prefix
        self.client_ref = client_ref
        self.loop = loop
        self.discovered = False
        # Map topic -> async callable that handles the message payload
        self.command_handlers: dict[str, "Any"] = {}

    def _state_topic(self, serial: str) -> str:
        return f"{self.node_prefix}/{serial}/state"

    def _availability_topic(self, serial: str) -> str:
        return f"{self.node_prefix}/{serial}/availability"

    def _discover(self, serial: str, sw_version: str | None) -> None:
        device = {
            "identifiers": [f"{self.node_prefix}_{serial}"],
            "manufacturer": "Reef Factory",
            "model": "KH Keeper",
            "name": "KH Keeper",
            "sw_version": sw_version or "unknown",
        }
        availability = {
            "availability_topic": self._availability_topic(serial),
            "payload_available": "online",
            "payload_not_available": "offline",
        }

        sensors = [
            # (object_id, name, value_template, unit, device_class, state_class, icon)
            ("kh", "KH", "{{ value_json.kh }}", "dKH", None, "measurement", "mdi:flask"),
            ("ph", "pH", "{{ value_json.ph }}", None, None, "measurement", "mdi:water-percent"),
            ("state", "State", "{{ value_json.state }}", None, None, None, "mdi:state-machine"),
            ("progress", "Measurement Progress", "{{ value_json.state_percent }}", "%", None, "measurement", "mdi:progress-clock"),
            ("last_alert", "Last Test Result", "{{ value_json.last_test_alert }}", None, None, None, "mdi:alert-circle-check"),
            ("last_test_time", "Last Test Time", "{{ value_json.last_test_time }}", None, "timestamp", None, "mdi:clock-outline"),
            ("next_test_time", "Next Test Time", "{{ value_json.next_test_time }}", None, "timestamp", None, "mdi:clock-start"),
            ("reagent_ml", "Reagent Remaining", "{{ value_json.reagent_ml }}", "mL", None, "measurement", "mdi:beaker"),
            ("interval", "Measurement Interval", "{{ value_json.interval }}", None, None, None, "mdi:timer-sand"),
            ("calibration_due", "Calibration Due", "{{ value_json.calibration_due }}", None, "date", None, "mdi:calendar-clock"),
            ("alarm_kh_low", "KH Alarm Low", "{{ value_json.alarm_kh_low }}", "dKH", None, None, "mdi:arrow-down-bold"),
            ("alarm_kh_high", "KH Alarm High", "{{ value_json.alarm_kh_high }}", "dKH", None, None, "mdi:arrow-up-bold"),
            ("adjustment", "KH Adjustment", "{{ value_json.adjustment }}", "dKH", None, None, "mdi:tune-vertical"),
        ]
        for object_id, name, tmpl, unit, dev_class, state_class, icon in sensors:
            payload = {
                "name": name,
                "unique_id": f"{self.node_prefix}_{serial}_{object_id}",
                "object_id": f"kh_keeper_{object_id}",
                "state_topic": self._state_topic(serial),
                "value_template": tmpl,
                "device": device,
                **availability,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if dev_class:
                payload["device_class"] = dev_class
            if state_class:
                payload["state_class"] = state_class
            if icon:
                payload["icon"] = icon

            topic = f"{self.discovery_prefix}/sensor/{self.node_prefix}_{serial}/{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)

        # Binary sensors for boolean alerts
        for object_id, name, tmpl, dev_class, icon in [
            ("reagent_low", "Reagent Low", "{{ 'ON' if value_json.reagent_low else 'OFF' }}", "problem", "mdi:flask-empty-outline"),
            ("calibration_warning", "Calibration Due Warning", "{{ 'ON' if value_json.calibration_warning else 'OFF' }}", "problem", "mdi:alert-octagon"),
        ]:
            payload = {
                "name": name,
                "unique_id": f"{self.node_prefix}_{serial}_{object_id}",
                "object_id": f"kh_keeper_{object_id}",
                "state_topic": self._state_topic(serial),
                "value_template": tmpl,
                "device": device,
                "device_class": dev_class,
                "icon": icon,
                **availability,
            }
            topic = f"{self.discovery_prefix}/binary_sensor/{self.node_prefix}_{serial}/{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)

        # Buttons (one-shot commands)
        buttons = [
            ("take_now", "Take KH Test Now", "mdi:test-tube",
             lambda _payload: self.client_ref.take_now()),
            ("cancel_measurement", "Cancel Measurement", "mdi:cancel",
             lambda _payload: self.client_ref.cancel_measurement()),
            ("dose_aquarium_test", "Aquarium Pump Accuracy Test (50 mL)", "mdi:water-pump",
             lambda _payload: self.client_ref.dose_aquarium_test(50.0)),
            ("dose_reagent_test", "Reagent Pump Accuracy Test (5 mL)", "mdi:beaker-plus",
             lambda _payload: self.client_ref.dose_reagent_test(5.0)),
        ]
        for object_id, name, icon, handler in buttons:
            cmd_topic = f"{self.node_prefix}/{serial}/cmd/{object_id}"
            payload = {
                "name": name,
                "unique_id": f"{self.node_prefix}_{serial}_{object_id}",
                "object_id": f"kh_keeper_{object_id}",
                "command_topic": cmd_topic,
                "device": device,
                "icon": icon,
                **availability,
            }
            topic = f"{self.discovery_prefix}/button/{self.node_prefix}_{serial}/{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)
            self.client.subscribe(cmd_topic)
            self.command_handlers[cmd_topic] = handler

        # Number entities (settable values)
        numbers = [
            ("calibrate_kh", "Calibrate KH from Drop Test", "dKH", 5.0, 15.0, 0.05,
             "{{ value_json.kh }}", "mdi:test-tube-empty",
             lambda payload: self.client_ref.calibrate_from_drop_test(float(payload))),
            ("set_adjustment", "KH Adjustment (raw)", "dKH", -5.0, 5.0, 0.01,
             "{{ value_json.adjustment }}", "mdi:tune-vertical",
             lambda payload: self.client_ref.set_adjustment(float(payload))),
            ("set_alarm_low", "KH Alarm Low (settable)", "dKH", 5.0, 15.0, 0.05,
             "{{ value_json.alarm_kh_low }}", "mdi:arrow-down-bold",
             self._make_alarm_setter("low")),
            ("set_alarm_high", "KH Alarm High (settable)", "dKH", 5.0, 15.0, 0.05,
             "{{ value_json.alarm_kh_high }}", "mdi:arrow-up-bold",
             self._make_alarm_setter("high")),
        ]
        for object_id, name, unit, mn, mx, step, value_template, icon, handler in numbers:
            cmd_topic = f"{self.node_prefix}/{serial}/cmd/{object_id}"
            payload = {
                "name": name,
                "unique_id": f"{self.node_prefix}_{serial}_{object_id}",
                "object_id": f"kh_keeper_{object_id}",
                "command_topic": cmd_topic,
                "state_topic": self._state_topic(serial),
                "value_template": value_template,
                "min": mn,
                "max": mx,
                "step": step,
                "unit_of_measurement": unit,
                "mode": "box",
                "device": device,
                "icon": icon,
                **availability,
            }
            topic = f"{self.discovery_prefix}/number/{self.node_prefix}_{serial}/{object_id}/config"
            self.client.publish(topic, json.dumps(payload), retain=True)
            self.client.subscribe(cmd_topic)
            self.command_handlers[cmd_topic] = handler

        # Select entity for measurement interval
        interval_options = ["1 hour", "2 hours", "4 hours", "6 hours", "8 hours",
                            "12 hours", "Off"]
        interval_label_to_code = {"1 hour": 0, "2 hours": 1, "4 hours": 2,
                                  "6 hours": 6, "8 hours": 3, "12 hours": 4, "Off": 5}
        interval_topic = f"{self.node_prefix}/{serial}/cmd/set_interval"
        interval_payload = {
            "name": "Measurement Interval (settable)",
            "unique_id": f"{self.node_prefix}_{serial}_set_interval",
            "object_id": "kh_keeper_set_interval",
            "command_topic": interval_topic,
            "state_topic": self._state_topic(serial),
            "value_template": "{{ value_json.interval }}",
            "options": interval_options,
            "device": device,
            "icon": "mdi:timer-sand",
            **availability,
        }
        topic = f"{self.discovery_prefix}/select/{self.node_prefix}_{serial}/set_interval/config"
        self.client.publish(topic, json.dumps(interval_payload), retain=True)
        self.client.subscribe(interval_topic)

        async def _set_interval_handler(payload: str):
            label = payload.strip()
            code = interval_label_to_code.get(label)
            if code is None:
                LOGGER.warning("Unknown interval option: %s", label)
                return
            await self.client_ref.set_interval(code)

        self.command_handlers[interval_topic] = lambda p: _set_interval_handler(p)

        # Mark online
        self.client.publish(self._availability_topic(serial), "online", retain=True)
        LOGGER.info("Published HA discovery: %d sensors + 2 binary_sensors + 4 buttons + 3 numbers + 1 select",
                    len(sensors))

    def _make_alarm_setter(self, which: str):
        # We need both low and high to send alarms, so we read the most recent
        # state from the client and substitute the changed one.
        async def _set(payload: str):
            try:
                new_val = float(payload)
            except ValueError:
                LOGGER.warning("Bad alarm payload: %r", payload)
                return
            state = self.client_ref.last_state
            low = state.get("alarm_kh_low")
            high = state.get("alarm_kh_high")
            if low is None or high is None:
                LOGGER.warning("Don't have current alarms yet; skipping")
                return
            if which == "low":
                low = new_val
            else:
                high = new_val
            await self.client_ref.set_alarms(low, high)
        return lambda p: _set(p)

    def _on_message(self, client, userdata, msg) -> None:
        # Runs in the paho-mqtt network thread; bridge over to asyncio.
        handler = self.command_handlers.get(msg.topic)
        if handler is None:
            return
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            payload = ""
        LOGGER.info("← MQTT command on %s: %r", msg.topic, payload)
        asyncio.run_coroutine_threadsafe(handler(payload), self.loop)

    async def __call__(self, state: dict, serial: str, sw_version: str | None) -> None:
        if not self.discovered:
            self._discover(serial, sw_version)
            self.discovered = True
        publishable = {k: v for k, v in state.items() if k != "history"}
        self.client.publish(self._state_topic(serial), json.dumps(publishable), retain=True)
        LOGGER.info("Published: KH=%s pH=%s state=%s%s reagent=%smL",
                    state.get("kh"), state.get("ph"),
                    state.get("state"),
                    f" ({state['state_percent']}%)" if state.get("state_percent") else "",
                    state.get("reagent_ml"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args) -> None:
    loop = asyncio.get_running_loop()

    async def _noop_handler(*_a, **_kw):  # placeholder, replaced for MQTT mode
        return None

    handler = print_handler if args.test else _noop_handler
    client = KHKeeperClient(args.host, handler)

    if not args.test:
        publisher = MQTTPublisher(
            host=args.mqtt_host,
            port=args.mqtt_port,
            user=args.mqtt_user,
            password=args.mqtt_pass,
            client_ref=client,
            loop=loop,
            discovery_prefix=args.discovery_prefix,
        )
        client.on_state = publisher

    # Schedule any one-shot CLI commands once the client has joined
    one_shots: list = []
    if args.trigger_test:
        one_shots.append(("take_now", lambda: client.take_now()))
    if args.cancel_measurement:
        one_shots.append(("cancel", lambda: client.cancel_measurement()))
    if args.calibrate_kh is not None:
        one_shots.append((f"calibrate_kh={args.calibrate_kh}",
                          lambda: client.calibrate_from_drop_test(args.calibrate_kh)))
    if args.set_adjustment is not None:
        one_shots.append((f"set_adjustment={args.set_adjustment}",
                          lambda: client.set_adjustment(args.set_adjustment)))
    if args.set_alarms is not None:
        low, high = args.set_alarms
        one_shots.append((f"set_alarms={low}/{high}",
                          lambda: client.set_alarms(low, high)))
    if args.set_interval is not None:
        one_shots.append((f"set_interval={args.set_interval}",
                          lambda: client.set_interval(args.set_interval)))
    if args.dose_aquarium_test:
        one_shots.append(("dose_aquarium_test", lambda: client.dose_aquarium_test(50.0)))
    if args.dose_reagent_test:
        one_shots.append(("dose_reagent_test", lambda: client.dose_reagent_test(5.0)))

    if one_shots:
        async def _run_one_shots():
            await client.wait_until_ready()
            await asyncio.sleep(2)
            for label, fn in one_shots:
                LOGGER.info("→ one-shot: %s", label)
                await fn()
        asyncio.create_task(_run_one_shots())

    await client.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="KH Keeper IP address (e.g. 192.168.1.229)")
    parser.add_argument("--test", action="store_true", help="Print decoded values, skip MQTT")
    parser.add_argument("--trigger-test", action="store_true",
                        help="Send a manual KH-test command shortly after connecting")
    parser.add_argument("--cancel-measurement", action="store_true",
                        help="Cancel an in-progress KH measurement")
    parser.add_argument("--calibrate-kh", type=float, default=None, metavar="DKH",
                        help="Calibrate KH from a drop-test reading (5-15 dKH). The bridge "
                             "reads the device's current KH + adjustment, computes the new "
                             "adjustment, and applies it.")
    parser.add_argument("--set-adjustment", type=float, default=None, metavar="DKH",
                        help="Set absolute KH adjustment in dKH (signed). Use 0 to reset. "
                             "Prefer --calibrate-kh unless you know the exact offset.")
    parser.add_argument("--set-alarms", type=float, nargs=2, default=None,
                        metavar=("LOW", "HIGH"),
                        help="Set KH alarm low and high thresholds (dKH)")
    parser.add_argument("--set-interval", type=int, default=None, metavar="CODE",
                        help="Set measurement interval. 0=1h 1=2h 2=4h 3=8h 4=12h 5=Off 6=6h")
    parser.add_argument("--dose-aquarium-test", action="store_true",
                        help="Run aquarium pump for 50 mL (accuracy verification)")
    parser.add_argument("--dose-reagent-test", action="store_true",
                        help="Run reagent pump for 5 mL (accuracy verification)")
    parser.add_argument("--mqtt-host", help="MQTT broker host (required unless --test)")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-user")
    parser.add_argument("--mqtt-pass")
    parser.add_argument("--discovery-prefix", default="homeassistant",
                        help="HA MQTT discovery prefix (default: homeassistant)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.test and not args.mqtt_host:
        parser.error("--mqtt-host required (or pass --test)")

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
