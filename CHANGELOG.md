# Changelog

## 0.1.8 — manual & automatic state refresh

- New **Refresh State** HA button. Re-sends `khConnect/join` to force the device to broadcast a fresh `khRefresh/settings` frame, which carries every diagnostic field (used_water_ml, waste_*, mixer, alarms, calibration, etc.).
- **Auto-refresh after every action.** `measure_ph`, `empty_cuvette`, and `fill_cuvette` now queue a state refresh immediately after the command, so all the *other* sensors (everything except pH) update too. pH already updates fine via the dedicated `khRefresh/pH` frame the device sends back from measurePh — the issue was the rest of the diagnostics looking frozen because settings frames are only auto-pushed on join, end-of-measurement, and doseAquarium.

## 0.1.7 — diagnostic sensors and buttons

- **Empty Cuvette** and **Fill Cuvette 50 mL** buttons re-added under the diagnostics panel for debugging. Prefixed `DIAG:` and grouped with the pump-accuracy tests, so easy to find but unlikely to be tapped accidentally during normal use. The pH-probe-must-stay-wet warning still applies — use them with care.
- Surfaces every parsed-but-previously-hidden field from the device's settings frame as a HA diagnostic sensor:
  - **Used Water (lifetime)** — cumulative aquarium water pumped (in mL). Watch this tick up by ~50 after each Refresh pH.
  - **Used Water (legacy counter)** — older per-test water counter from the device.
  - **Waste Current / Waste Limit** — waste-tank fill state.
  - **Remeasure Threshold** — dKH delta that triggers an automatic remeasurement.
  - **Mixer Speed** — Slow/Medium/Fast.
  - **State Code (raw) / State % (raw) / Interval Code (raw)** — raw protocol values, useful for debugging state-machine behaviour.
  - **Water Return / Internal Light** — boolean device flags.
- Marked with `entity_category: diagnostic` so they appear under the device's diagnostics panel in HA, not in the main entities view.
- Lets you watch the device's behaviour live as you press buttons — particularly Used Water (lifetime), which is the most reliable proof that doseAquarium actually pumped.

## 0.1.6 — empty-cuvette payload fix + observability

- **Fixed `khCommand/empty` payload length** — was 4 bytes, should be 5 (`00 0a ae 60 00`). Verified against HAR capture: empty frame is 53 bytes, doseAquarium frame is 59 bytes, the 7-char subcommand-name difference accounts for only 6 bytes — the missing byte was a leading null in the empty payload. Sending the wrong length almost certainly caused the device to silently no-op the drain.
- **pH change logging** — when a `khRefresh/pH` frame arrives, the bridge now logs `pH UPDATE: 8.13 → 8.34` (or `pH unchanged` if the value didn't move). Makes it easy to spot whether a Refresh pH actually got fresh tank water in the cuvette.
- **used_water_ml delta logging** — when settings frames arrive, log any change in cumulative water-used. Confirms doseAquarium physically pumped (state stays Idle through these ops, so you can't tell from state alone).
- Note: state staying at "Idle" through empty/doseAquarium/measurePh is normal device behaviour — the device treats these as instant from the state-machine's POV. Verified against the web UI's HAR captures.

## 0.1.5 — proper pH refresh (HAR-verified bytes)

- Captured exact byte payloads from the device's web UI HAR export. Replaces the previous guesswork:
  - `khCommand/empty` takes a 4-byte magic payload `0a ae 60 00` (likely drain duration). Previous empty-payload version was wrong and silently no-op'd.
  - `khCommand/doseAquarium` takes 4-byte fixed-point mL × 10000 (already correct).
  - `khCommand/measurePh` is a dedicated "read pH now" command — empty payload, returns a `khRefresh/pH` frame within ~1s. **This is the lightweight pH refresh we were looking for.**
- New `Refresh pH` HA button: empty → fill 50 mL fresh tank water → measurePh. ~50s total, no reagent. Replaces the broken 0.1.3 "Rinse Cuvette" sequence.
- `Measure pH` button (reads current cuvette water — useful right after a real KH test or manual refill).
- **Empty / Fill standalone buttons intentionally NOT exposed in HA** — the pH probe should not sit dry, so we don't expose primitives that could leave it that way. Available via CLI for diagnostics.
- Refresh pH sequence queues empty + fill back-to-back to minimize the dry window on the probe.
- **All pump/measurement commands now refuse to run while the device is non-Idle** (e.g. mid-KH test). Pressing Refresh pH or Measure pH during a real measurement was observed to abort the test and drop the WS connection. Bridge now logs a warning and skips instead.
- **Reconnect backoff resets on transient drops.** The device cycles its WS connection ~5s after some commands (e.g. measurePh) — observed firmware behaviour, can't be fixed from our side. Previously the bridge would wait up to 60s before reconnecting. Now if we were actively connected before the drop, we reconnect within 1s.
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
