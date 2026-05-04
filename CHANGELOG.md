# Changelog

## 0.1.4 — proper pH refresh (HAR-verified bytes)

- Captured exact byte payloads from the device's web UI HAR export. Replaces the previous guesswork:
  - `khCommand/empty` takes a 4-byte magic payload `0a ae 60 00` (likely drain duration). Previous empty-payload version was wrong and silently no-op'd.
  - `khCommand/doseAquarium` takes 4-byte fixed-point mL × 10000 (already correct).
  - `khCommand/measurePh` is a dedicated "read pH now" command — empty payload, returns a `khRefresh/pH` frame within ~1s. **This is the lightweight pH refresh we were looking for.**
- New `Refresh pH` HA button: empty → fill 50 mL fresh tank water → measurePh. ~50s total, no reagent. Replaces the broken 0.1.3 "Rinse Cuvette" sequence.
- `Measure pH` button (reads current cuvette water — useful right after a real KH test or manual refill).
- **Empty / Fill standalone buttons intentionally NOT exposed in HA** — the pH probe should not sit dry, so we don't expose primitives that could leave it that way. Available via CLI for diagnostics.
- Refresh pH sequence queues empty + fill back-to-back to minimize the dry window on the probe.
- Bridge now sends `get/user` after `khConnect/join` to match the web UI's session handshake.
- **Optional periodic pH refresh** via the new `ph_refresh_interval_minutes` add-on option (default 0 = off). Set e.g. `60` to refresh pH every hour, independent of the device's KH test cadence (which runs on its own schedule, e.g. 4-hourly). The scheduler skips the refresh if the device is busy with a real measurement so it never collides.
- CLI: `--measure-ph`, `--refresh-ph ML`, `--fill-cuvette ML`, `--ph-refresh-interval MIN` flags.

## 0.1.3 — rinse cuvette / refresh pH

- Add `khCommand/empty`, `khCommand/circuitStartAquarium`, and `khSet/return` to the bridge (captured from the device's web UI).
- New high-level `Rinse Cuvette / Refresh pH` HA button: replicates the app's "Empty cuvette" sequence (empty → refill with fresh tank water). After refill the device pushes a fresh pH reading, so this gives an updated pH without burning reagent on a full KH test.
- `Empty Cuvette (drain only)` button for manual maintenance.
- CLI: `--empty-cuvette` and `--rinse-cuvette ML` flags for one-shot testing.

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
