# Changelog

> **Compatibility convention:** every release entry below states the HA Core (and HAOS, when relevant) versions it was developed and tested against. **Compatibility is verified on those versions only.** Upgrading HA past the listed version isn't guaranteed to work — check the next release for an updated compat line before upgrading. If you want to upgrade HA first and don't see a release here that lists the new version, hold off or test on a non-prod instance.

## 0.1.14 — Fix: refresh-pH cycle was clobbering `ph` (water+reagent) with the pure-water reading

**Tested against:** HA Core `2026.4.4` (dev + prod), Supervisor `2026.04.2`, HAOS `17.2`. Add-on slot: `kh_keeper_bridge`. MQTT broker: Mosquitto add-on (any 2.x).

### Bug

User reported on prod that the "Refresh pH" routine was updating the wrong sensor — the regular `KH Keeper pH` (water+reagent KH-test cuvette pH) was getting overwritten with the pure-water reading instead of routing to `KH Keeper pH (Pure Tank Water)`.

### Root cause

In `_handle_frame`'s pH-frame branch (`kh_keeper_bridge.py:843`), the bridge unconditionally wrote the incoming pH value to `last_state["ph"]` BEFORE checking the `_next_ph_is_pure` flag — then ALSO wrote to `last_state["ph_pure"]` when the flag was set. So during a refresh_ph cycle the pure-water pH ended up in both fields, clobbering the last KH-test cuvette pH.

The existing tests in `test_ph_routing.py` were inline mock-replicas of the intended logic — they didn't actually call the real handler, so they reproduced the buggy code path AND asserted the buggy result (the assertions said `ph == pure_value` after a pure cycle, exactly what the bug produced).

### Fix

`_handle_frame` now partitions the routing properly:
- `_next_ph_is_pure=True` → write ONLY to `ph_pure`, leave `ph` untouched, clear the phase tracker
- `_next_ph_is_pure=False` → write to `ph` (water+reagent assumed), leave `ph_pure` untouched

The standalone `measure_ph` button's docstring is updated to clarify routing: it always writes to `ph`. Users who want a pure-water reading should call `refresh_ph` instead, which sequences the empty + refill + measure + correct routing.

### Tests

`tests/test_ph_routing.py` — three existing tests rewritten to call the real `_handle_frame` with properly-encoded frames (instead of inline mocks), so future regressions in the routing logic are caught. New `test_pure_pH_frame_writes_only_ph_pure_not_ph` is the bug-fix regression test that seeds a prior `ph` value, runs a pure cycle, and asserts `ph` is unchanged.

48 passing.

## 0.1.13 — refresh-pH progress visibility + 0.1.12 entity-name correction

**Tested against:** HA Core `2026.4.4` (dev + prod), Supervisor `2026.04.2`, HAOS `17.2`. Add-on slot: `kh_keeper_bridge`. MQTT broker: Mosquitto add-on (any 2.x).

The 0.1.12 `refresh_ph` cycle is silent for ~10 minutes between the empty/fill commands and the final pH measurement. Users had no way to tell whether the cycle was actively running, stuck, or failed. 0.1.13 adds two MQTT-discovered sensors that surface the current phase and an ETA timestamp, so a dashboard tile can show "draining (eta in 4 minutes)" / "filling (eta in 5 minutes)" / "measuring" / "idle".

- **`sensor.kh_keeper_refresh_ph_phase`** — current phase as a string: `idle`, `draining`, `filling`, `measuring`. Updates immediately when each phase begins (not waiting for the next settings frame).
- **`sensor.kh_keeper_refresh_ph_phase_eta`** — ISO-8601 timestamp (TIMESTAMP device class) of when the current phase is expected to complete. Populated for `draining` (now + 5min) and `filling` (now + 5min); null for `measuring` (device responds in ~1s) and `idle`.
- Phase is reset to `idle` when the pure-water `khRefresh/pH` frame is consumed by the cycle (so the dashboard tile clears automatically). Manual `Measure pH` presses do not touch the phase — leaves an in-progress refresh_ph indicator alone.

**0.1.12 entity-name correction (informational, no code change in 0.1.12):** the actual entity_ids that prod registered after install were:

- `sensor.kh_keeper_ph_pure_tank_water` (not `sensor.kh_keeper_ph_pure`)
- `sensor.kh_keeper_ph_kh_test_water_reagent` (not `sensor.kh_keeper_ph_kh_test`)

This is because HA's MQTT discovery generates entity_ids from the `name` (friendly name), not the `object_id` field we specify in the discovery payload. The shorter names in the original 0.1.12 CHANGELOG were aspirational; the actual entity_ids match the friendly names. Any reeftanktracker `auto_source` config should target `sensor.kh_keeper_ph_pure_tank_water`.

**Test infrastructure (recovered from 0.1.12 PR — was missed in the original merge):** added 48 pytest cases (was 0) covering pH frame routing, cuvette timing, refresh_ph orchestration, settings parser, drop-test calibration, payload encoders, idle gating, frame protocol round-trip, and the new phase tracking. Plus `pyproject.toml` with pytest-asyncio config and a GitHub Actions tests workflow. CI runs on push + PR against Python 3.11 and 3.12.

## 0.1.12 — split pH sensors + correct cuvette cycle timing

**Tested against:** HA Core `2026.4.4` (dev) and `2026.4.4` (prod, installed). Supervisor `2026.04.2`. HAOS `17.2` (latest at time of release: `17.3`; HA Core latest: `2026.5.0` — release not yet verified against either). Add-on slot: `kh_keeper_bridge`. MQTT broker: Mosquitto add-on (any 2.x).

Two related fixes around the pH measurement procedure:

**Cuvette cycle timing.** `refresh_ph` previously queued empty + fill back-to-back and slept 55s before triggering measurePh. Real device timing is ~5 minutes per pump operation (drain + fill = ~10 minutes total), and the device is silent and ignores commands during each phase. The 55s wait was wildly insufficient — measurePh frequently fired against still-stale water from the prior KH test, producing the "pH unchanged at X (cuvette water didn't refresh?)" log noise. Now: queue empty → wait 300s → queue fill → wait 300s → queue measurePh. New `EMPTY_DURATION_S` and `FILL_DURATION_S` class constants document the timing.

**Split pH into two sensors so you stop conflating two different solutions.** A pH from a KH test is the pH of tank water + alkalinity reagent (different solution from pure tank water). Today both readings landed on the single `sensor.kh_keeper_ph` entity, which made it useless as an aquarium-pH advisor input. Now:

- **`sensor.kh_keeper_ph_pure_tank_water`** — populated only by `refresh_ph` (empty + fill fresh tank water + measurePh). This is the correct input for any aquarium pH advisor or dashboard tile that represents the tank's pH. Friendly name: "pH (Pure Tank Water)".
- **`sensor.kh_keeper_ph_kh_test_water_reagent`** — populated only when a NEW KH test's `history[0].timestamp` differs from the last consumed one. Friendly name: "pH (KH Test, Water + Reagent)". Useful for diagnostics (detecting reagent issues, batch drift) but DO NOT substitute for pure-water pH in advisor logic. Marked diagnostic.
- **`sensor.kh_keeper_ph`** — kept for back-compat. Reflects whatever the device last reported; semantics unchanged. New consumers should use one of the two split sensors.

> **Entity_id note:** HA's MQTT discovery generates entity_ids from the friendly `name`, not the `object_id` field. The `object_id` we specify (`ph_pure`, `ph_kh_test`) is ignored, so the actual entity_ids end up matching the friendly name (`ph_pure_tank_water`, `ph_kh_test_water_reagent`). Document accordingly.

To get pH advisor working correctly, swap the `auto_source` for the `pH` parameter in reeftanktracker from `sensor.kh_keeper_ph` → `sensor.kh_keeper_ph_pure_tank_water`.

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
