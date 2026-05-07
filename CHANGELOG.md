# Changelog

> **Compatibility convention:** every release entry below states the HA Core (and HAOS, when relevant) versions it was developed and tested against. **Compatibility is verified on those versions only.** Upgrading HA past the listed version isn't guaranteed to work — check the next release for an updated compat line before upgrading. If you want to upgrade HA first and don't see a release here that lists the new version, hold off or test on a non-prod instance.

## 0.1.12 — split pH sensors + correct cuvette cycle timing

**Tested against:** HA Core `2026.4.4` (dev) and `2026.4.4` (prod, installed). Supervisor `2026.04.2`. HAOS `17.2` (latest at time of release: `17.3`; HA Core latest: `2026.5.0` — release not yet verified against either). Add-on slot: `kh_keeper_bridge`. MQTT broker: Mosquitto add-on (any 2.x).

Two related fixes around the pH measurement procedure:

**Cuvette cycle timing.** `refresh_ph` previously queued empty + fill back-to-back and slept 55s before triggering measurePh. Real device timing is ~5 minutes per pump operation (drain + fill = ~10 minutes total), and the device is silent and ignores commands during each phase. The 55s wait was wildly insufficient — measurePh frequently fired against still-stale water from the prior KH test, producing the "pH unchanged at X (cuvette water didn't refresh?)" log noise. Now: queue empty → wait 300s → queue fill → wait 300s → queue measurePh. New `EMPTY_DURATION_S` and `FILL_DURATION_S` class constants document the timing.

**Split pH into two sensors so you stop conflating two different solutions.** A pH from a KH test is the pH of tank water + alkalinity reagent (different solution from pure tank water). Today both readings landed on the single `sensor.kh_keeper_ph` entity, which made it useless as an aquarium-pH advisor input. Now:

- **`sensor.kh_keeper_ph_pure`** — populated only by `refresh_ph` (empty + fill fresh tank water + measurePh). This is the correct input for any aquarium pH advisor or dashboard tile that represents the tank's pH. Friendly name: "pH (Pure Tank Water)".
- **`sensor.kh_keeper_ph_kh_test`** — populated only when a NEW KH test's `history[0].timestamp` differs from the last consumed one. Friendly name: "pH (KH Test, Water + Reagent)". Useful for diagnostics (detecting reagent issues, batch drift) but DO NOT substitute for pure-water pH in advisor logic. Marked diagnostic.
- **`sensor.kh_keeper_ph`** — kept for back-compat. Reflects whatever the device last reported; semantics unchanged. New consumers should use one of the two split sensors.

To get pH advisor working correctly, swap the `auto_source` for the `pH` parameter in reeftanktracker from `sensor.kh_keeper_ph` → `sensor.kh_keeper_ph_pure`.

## 0.1.11 — pH no-clobber, debugged

- Fix the 0.1.10 no-clobber not actually preventing pH overwrites in some cases. Was only kicking in when `last_test_iso` was already set; now also handles the first-settings-after-boot case.
- INFO log when we keep a live pH against a stale settings value, so you can tell from the log that the protection fired.
- DEBUG log of the comparison values for diagnosing edge cases.

## 0.1.10 — water counter logging + pH no-clobber

- Log deltas on `used_water_ml`, `waste_current_ml`, and `used_water_ml_v0` whenever a settings frame ticks them. Lets you see in the bridge log whether empty/fill commands actually moved water (without needing visual on the device or a HA dashboard refresh).
- Fix pH/kh getting overwritten by history values when a settings frame arrives. Settings carries the LAST completed KH test's results in `history[0]` — for measurements made between full KH tests (e.g. via Refresh pH / Measure pH), we now keep the live value rather than reverting to the older test reading.

## 0.1.9 — refresh state without crashing the device

- 0.1.8's Refresh State sent `khConnect/join` to force a fresh settings push. **This crashes the device's network stack hard enough to require a power cycle** (the device stops responding to all WS clients and eventually drops off the network with Errno 113 / no route to host).
- Switched to `khSet/settings` written with the *current* alarm values — an idempotent no-op write that the device replies to with a full settings broadcast. Safe.
- Same auto-refresh after empty/fill/measurePh — now uses the safe path.
- **If your device is currently unreachable**: power-cycle it before deploying 0.1.9.

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
