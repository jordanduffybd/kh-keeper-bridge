#!/usr/bin/with-contenv bashio
# Entrypoint for the KH Keeper Bridge HA add-on.
# Reads add-on config + MQTT service info from the supervisor and starts the bridge.

set -e

DEVICE_HOST=$(bashio::config 'device_host')
LOG_LEVEL=$(bashio::config 'log_level')
TRIGGER_ON_START=$(bashio::config 'trigger_test_on_start')

bashio::log.info "KH Keeper IP: ${DEVICE_HOST}"
bashio::log.info "Log level: ${LOG_LEVEL}"

# Pull MQTT broker info from HA's supervisor (Mosquitto add-on or external)
if ! bashio::services.available "mqtt"; then
    bashio::exit.nok "No MQTT service available. Install the Mosquitto add-on first."
fi

MQTT_HOST=$(bashio::services mqtt "host")
MQTT_PORT=$(bashio::services mqtt "port")
MQTT_USER=$(bashio::services mqtt "username")
MQTT_PASS=$(bashio::services mqtt "password")

bashio::log.info "MQTT broker: ${MQTT_HOST}:${MQTT_PORT}"

ARGS=(
    --host "${DEVICE_HOST}"
    --mqtt-host "${MQTT_HOST}"
    --mqtt-port "${MQTT_PORT}"
)
if [[ -n "${MQTT_USER}" ]]; then
    ARGS+=(--mqtt-user "${MQTT_USER}")
fi
if [[ -n "${MQTT_PASS}" ]]; then
    ARGS+=(--mqtt-pass "${MQTT_PASS}")
fi
if [[ "${LOG_LEVEL}" == "debug" ]]; then
    ARGS+=(-v)
fi
if bashio::var.true "${TRIGGER_ON_START}"; then
    ARGS+=(--trigger-test)
fi

exec python3 /app/kh_keeper_bridge.py "${ARGS[@]}"
