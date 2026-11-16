# Changelog

## 0.9.0 (first public version)

- English code, logs, MQTT payloads, states and event types. New `language` option
  (`en`/`es`) for entity names, schedule text and event details.
- New entities get predictable ids (`climate.watts_zone_1`, `sensor.watts_zone_1_temperature`…)
  through `default_entity_id`. Existing entities keep theirs.
- MQTT broker and credentials taken from the Mosquitto add-on when `mqtt_host` is empty.
- Removed the experimental parts: HTTPS listener on port 443, TLS interception, tcpdump
  sniffer and cloud heartbeat. The central unit only uses HTTP on port 80, and the add-on
  no longer needs extra network privileges.
- Schedule text accepts spaces around the dash (`06:00 – 09:30`) and English day names.
- Automated tests (Python and Node).

Upgrading from 0.8.x: the files in `/data` are migrated automatically, and the old MQTT
command topics (`set/horario`, `set/boost_minutos`) are still accepted. **Changed:** event
types (`orden_fallida` → `command_failed`, etc.), the attributes of events (`detalle` →
`detail`, `termostato` → `thermostat`, `zona` → `zone`), the states of *Connection*
(`nube` → `cloud`) and *Status* (`aviso`/`problema` → `warning`/`problem`), and the
attributes of the schedule entity (`semana` → `week`, `comando` → `command_topic`,
`orden_en_curso` → `pending`, `zona` → `zone`). Update automations and use the schedule
card 0.2.0.

## 0.8.0

- Health: communication with the central unit, warning per zone (silent thermostat or
  unidentified field), status entity and event entity.

## 0.7.x

- Editable weekly schedules (text entity) and the graphical schedule card.
- Frost protection published as the standard `away` preset.

## 0.6.x

- Full climate entity: off, eco, frost protection, boost with configurable duration.
- Boost from off goes through manual first (the thermostat ignores it otherwise).

## 0.5.x

- Local answers when the cloud is down, and the "Local mode" switch.

## 0.3.0 – 0.4.0

- MQTT discovery (read only), then optional control from Home Assistant.

## 0.1.0 – 0.2.0

- Logging proxy used to understand the protocol.
