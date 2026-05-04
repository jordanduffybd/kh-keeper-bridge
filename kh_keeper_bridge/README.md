# KH Keeper Bridge (Home Assistant add-on)

Bridges a Reef Factory KH Keeper to Home Assistant via MQTT auto-discovery.

## What you get

**Sensors**

- KH (dKH)
- pH
- Measurement state (Idle / Measuring / Calibrating / Cancelling / Remeasuring)
- Measurement progress %
- Last test time, last test result
- Next test time
- Reagent remaining (mL)
- Calibration due date
- KH alarm low / high (current values)
- Current KH adjustment

**Binary sensors**

- Reagent low alert
- Calibration due warning

**Buttons**

- Take KH test now
- Cancel measurement
- Aquarium pump accuracy test (50 mL)
- Reagent pump accuracy test (5 mL)

**Number entities** (settable from HA)

- Calibrate KH from drop test (enter your test-kit reading; bridge applies the right offset)
- KH adjustment (raw signed offset)
- KH alarm low / high

**Select entity**

- Measurement interval (1h / 2h / 4h / 6h / 8h / 12h / Off)

## Configuration

```yaml
device_host: 192.168.1.229   # IP of your KH Keeper on the LAN
log_level: info              # debug | info | warning | error
trigger_test_on_start: false # if true, fires a manual test 2s after start
```

The add-on auto-discovers your MQTT broker via the supervisor (Mosquitto add-on
or the MQTT integration). No need to enter MQTT host/user/pass manually.

## Requirements

- Home Assistant OS or Supervised (so the add-on framework is available)
- Mosquitto add-on (or another MQTT broker) installed and running
- The KH Keeper accessible on your LAN (must be on the same network as HA)

## Notes

- Full pump CALIBRATION (the multi-step "start → run → enter actual mL → device
  computes ratio" flow) is **not** exposed — it's awkward in HA. Use the
  device's web UI or the Smart Reef app for that. The add-on does expose the
  *accuracy test* buttons so you can verify a calibration after the fact.
- The KH Keeper does **not** measure water temperature. If you want temp in HA,
  pair a Reef Factory Smart Temperature device — that's a separate integration.
