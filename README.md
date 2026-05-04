# KH Keeper Bridge

A Home Assistant add-on that bridges a [Reef Factory KH Keeper](https://reeffactory.com/) to Home Assistant via MQTT auto-discovery, using the device's local WebSocket protocol — no cloud account, no Selenium.

## Why this exists

Reef Factory's KH Keeper has no official local API. The web UI on the device pulls live data via a binary WebSocket. This add-on speaks that same protocol directly, decodes the frames, and republishes them as native MQTT entities so HA picks them up automatically.

## What's exposed

Sensors for KH, pH, status, measurement progress, reagent level, calibration due, last/next test, alarm thresholds, and current adjustment. Buttons to trigger or cancel a test and run pump accuracy checks. Number entities to recalibrate from a drop test reading or change alarm thresholds. A select to change measurement interval.

See [`kh_keeper_bridge/README.md`](kh_keeper_bridge/README.md) for the full list and configuration options.

## Install in Home Assistant

1. **Settings → Add-ons → Add-on Store**, then top-right menu → **Repositories**.
2. Add this repo's URL: `https://github.com/jordanduffy/kh-keeper-bridge`
3. Install the **KH Keeper Bridge** add-on.
4. Configure `device_host` (your KH Keeper's LAN IP).
5. Start. The Mosquitto add-on must be running.

Sensors will appear under a new "KH Keeper" device in Settings → Devices & Services.

## Requirements

- Home Assistant OS or HA Supervised
- Mosquitto add-on (or another MQTT broker)
- KH Keeper reachable on your LAN

## Run standalone (without HA)

The bridge is also a self-contained Python script. See [`kh_keeper_bridge/kh_keeper_bridge.py`](kh_keeper_bridge/kh_keeper_bridge.py) — `--help` lists all CLI flags including test mode for verifying values without MQTT.

```bash
pip install -r kh_keeper_bridge/requirements.txt
python3 kh_keeper_bridge/kh_keeper_bridge.py --host 192.168.1.229 --test
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Protocol details reverse-engineered from the device's own JS source. Reef Factory has stated they will not be adding an official API.
