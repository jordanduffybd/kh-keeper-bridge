# Changelog

## 0.1.2 — stuck-state fix

- **Detect dead WebSocket connections.** The bridge would happily send pings into a half-dead TCP socket forever (kernel buffers writes, device never replies), leaving HA stuck on the last state. Enabled the websockets library's protocol-level keepalive (`ping_interval=20`, `ping_timeout=15`) so dead connections trigger a reconnect within ~35s.
- **Frame watchdog** as a second layer: if no frame received from the device in 5 minutes, force a reconnect.
- **Handle `khRefresh/pH` frames** the device sends between full settings updates — pushes live pH to MQTT so the dashboard stays current.

## 0.1.1 — timestamp fix

- Fix `Last Test Time` and `Next Test Time` showing as Unknown in HA — timestamps now serialize as timezone-aware ISO 8601 (HA's `device_class: timestamp` rejects naive datetimes).

## 0.1.0 — initial release

- Reverse-engineered the KH Keeper local WebSocket protocol (path `/controler`, subprotocol `arduino`)
- Read sensors: KH, pH, status, measurement progress, reagent level, calibration due, last/next test, alarm thresholds, adjustment
- Binary sensors: reagent low, calibration due
- Buttons: take test now, cancel measurement, aquarium pump accuracy test (50 mL), reagent pump accuracy test (5 mL)
- Number entities: calibrate from drop test, raw KH adjustment, alarm low, alarm high
- Select entity: measurement interval
- HA add-on package with Mosquitto auto-discovery via supervisor
