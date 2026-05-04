# Changelog

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
