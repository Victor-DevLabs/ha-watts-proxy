# Watts Proxy

Local proxy between a **Watts Vision BT-CT02-RF** central unit and the Watts cloud
(`smarthome.wattselectronics.com`).

- It **forwards** every request of the central unit to the cloud, unchanged, so the
  Watts app keeps working.
- If the cloud does not answer, it **answers by itself** with the same format, so the
  central unit and Home Assistant keep working.
- It **publishes** the central unit and the thermostats to Home Assistant over MQTT,
  with optional control: modes, presets, setpoints, weekly schedules and health warnings.

Nothing is sent over the radio: the central unit keeps managing the thermostats as
usual. To undo everything, remove the DNS rewrite.

> **Beta.** Tested only with one BT-CT02-RF (firmware 03.63) and BT-D03-RF thermostats.
> Other Watts central units may use other fields. Feedback is welcome.

## How it works

The central unit talks **plain HTTP on port 80** (no TLS) to
`smarthome.wattselectronics.com`. A DNS rewrite makes that name point to Home Assistant,
**only for the central unit**. The proxy then sits between the central unit and the cloud:

```
central unit ──HTTP:80──> Watts Proxy (Home Assistant) ──> Watts cloud
                                │
                                └──MQTT──> Home Assistant entities
```

Commands from Home Assistant use the same channel as the app: every few seconds the
central unit asks the cloud for pending commands (`query/check`). When the answer is
"nothing pending", the proxy replaces it with the command, in the same format the
cloud uses. Commands coming from the cloud are never touched.

## Installation

1. Install the **Mosquitto broker** add-on and the **MQTT** integration, if you do not
   have them yet.
2. Add this repository: *Settings → Add-ons → Add-on store → ⋮ → Repositories*, and
   paste `https://github.com/Victor-DevLabs/ha-watts-proxy`.
3. Install **Watts Proxy**, set `language` and, if you want to control the thermostats,
   `allow_control: true`. Start it.
4. The log must show `listening on port 80`. If it fails with *address in use*, another
   add-on is using port 80 of the host.

Port 80 must be free on the Home Assistant host. The add-on uses the host network.

### Point the central unit to the proxy (DNS)

The central unit gets its DNS server from the router (DHCP); its own menu has no DNS
setting. You need a DNS server that can rewrite one name **for one client only**.
With the **AdGuard Home** add-on:

1. Make your router hand out AdGuard (the Home Assistant IP) as DNS, or at least give
   the central unit a fixed IP (DHCP reservation) and make sure it uses AdGuard.
2. Turn **protection on** in AdGuard (custom rules are not applied without it).
3. *Filters → Custom filtering rules*, add (replace the IPs):

   ```
   ||smarthome.wattselectronics.com^$dnsrewrite=NOERROR;A;<HOME_ASSISTANT_IP>,client=<CENTRAL_UNIT_IP>
   ```

4. In the query log, the queries of the central unit must show as *Rewritten*. The
   central unit asks every few seconds, so the change is almost immediate.

**Undo:** delete or comment out (`!`) the rule. The central unit goes back to the cloud
within seconds.

### Recommended: block the central unit from the Internet

If the router hands out a secondary DNS (for example `1.1.1.1`), the central unit
falls back to it from time to time (every 10–50 minutes in our tests) and talks to the
real cloud directly, bypassing the proxy for a while. Blocking the Internet access of
the central unit in the router (by MAC address) fixes that completely: it can only talk
to the proxy, and the proxy reaches the cloud on its behalf.

Keep in mind that, with the block, **if Home Assistant is down the central unit has no
cloud** (the thermostats keep working on their own). Remove the block to go back.

## Entities

Device **Watts Central**:

| Entity | Meaning |
|---|---|
| Heat demand (any zone) | On if any thermostat is calling for heat. |
| Zones with demand | Number of thermostats calling for heat. |
| Connection | `cloud` (forwarding) or `local` (the proxy answers). |
| Local mode (no cloud) | Switch: stop using the cloud, at runtime (see below). |
| Boost duration | Minutes of the boost preset (5–240, default 120). |
| Communication | Off if the central unit sent nothing for `central_timeout_min` minutes. |
| Status | `ok`, `warning` or `problem`; attribute `problems` lists the reasons. |
| Events | Event entity for automations (see below). |
| Last data | Time of the last message from the central unit. |

One device **Watts &lt;zone&gt;** per thermostat (zone names come from the central unit):

| Entity | Meaning |
|---|---|
| climate (with `allow_control`) | Modes, presets, setpoint, current temperature, heating or idle. |
| Temperature | Measured temperature. |
| Heat demand | The thermostat is calling for heat. |
| Next change | Next comfort/eco change of the program. |
| Schedule (with `allow_control`) | Editable weekly schedule (text). |
| Warning | Problem: no reports, or an unidentified field changed (see Health). |
| Diagnostic | Setpoint, comfort/eco/boost/frost setpoints, mode, program period, boost remaining, last report. |

New entities get ids like `climate.watts_zone_1` and `sensor.watts_zone_1_temperature`.

Entities become unavailable if the add-on stops, or if no data arrives for 15 minutes.

## Control (`allow_control: true`)

- **Modes:** *Off*, *Heat* (manual comfort) and *Auto* (weekly program).
- **Presets:** *Eco*, *Boost* and *Away* (= frost protection on the thermostat).
- **Setting a temperature** changes the setpoint of the current mode, as on the
  thermostat itself: in eco the eco setpoint (also used by the eco periods of the
  program), in boost the boost setpoint (keeping the remaining time), in frost
  protection the frost setpoint (max. 10 °C), and in manual, program or off it switches
  to manual with that setpoint, like the app. Range 5–30 °C in steps of 0.5 °C.
- **Immediate feedback:** Home Assistant shows the change right away. If the central
  unit does not confirm it after 3 attempts (45 s each), the real state comes back and
  a `command_failed` event is fired.
- **Boost from off:** the central unit accepts it, but the thermostat does not apply it.
  The proxy sends *manual* first and, once confirmed, the boost.

> A confirmation from the central unit does not always mean the thermostat applied the
> command (see above). The thermostats talk to the central unit by radio every few
> minutes; a change can take a while to show on the thermostat.

## Weekly schedules

Each zone has a **Schedule** text entity with the comfort periods of the week; the rest
of the time is eco. It is shown and edited in the same format:

```
Mon–Fri: 06:00–09:30, 13:00–15:30; Sat–Sun: all eco
```

- Groups separated by `;` or new lines, each `<days>: <periods>`.
- Days: `Mon Tue Wed Thu Fri Sat Sun` (Spanish names work too), ranges `Mon–Fri`,
  lists `Mon, Wed`, or `all`.
- Periods: `HH:MM–HH:MM` (or `-`), full or half hours only, up to `24:00`.
  `all eco` or `all comfort` for the whole day.
- **Days you do not write keep their schedule.** `Sat: 09:00–13:00` only changes Saturday.
- The raw 336 characters (`0` = eco, `5` = comfort) are accepted too.

The schedule is fully validated before anything is sent. If it is invalid, nothing is
sent and a `schedule_rejected` event explains why. The previous schedule is written to
`events.log`, so it can be restored.

### Graphical card

The repository includes `watts-schedule-card`, a graphical editor for these schedules:
a 7 × 48 grid you paint by dragging (mouse or touch), copy a day to Mon–Fri / Sat–Sun /
every day, save or discard, and a message when the central unit confirms the change.
See the [README](https://github.com/Victor-DevLabs/ha-watts-proxy#schedule-card) for
installation.

## Health and events

- **Communication** goes off if the central unit sends nothing for `central_timeout_min`
  minutes (default 5).
- **Warning** of a zone goes on if the thermostat sends no report for `zone_timeout_min`
  minutes (default 30) while the central unit does report, or if one of the unidentified
  fields `23`, `34`, `35` stops being 0, or a new field appears.
- **Battery:** nobody knows yet which local field means *low battery* (`error_code` in
  the cloud API). The Warning watches the candidates; if one of your thermostats reports
  low battery, the warning will tell which field changed. **Please report it.**

Event types of the **Events** entity: `command_confirmed`, `command_failed`,
`schedule_rejected`, `cloud_unavailable`, `cloud_restored` (real outages only, not the
forced local mode), `central_no_data`, `central_restored` and `thermostat_warning`.
Attributes: `detail` and, when relevant, `thermostat` and `zone`.

```yaml
triggers:
  - trigger: state
    entity_id: event.watts_central_events
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type in
         ['command_failed', 'thermostat_warning', 'central_no_data'] }}
actions:
  - action: notify.notify
    data:
      title: Heating
      message: "{{ trigger.to_state.attributes.detail }}"
```

## When the cloud is down

Each request is forwarded to the cloud and its answer awaited (`cloud_timeout`). If the
cloud does not answer, refuses the connection or returns something that is not a Watts
answer, the proxy answers by itself:

- `query/check` → "no pending commands" (commands from Home Assistant are still delivered);
- `edit` and `insert` requests → "received";
- `get_current_timestamp` → the time of Home Assistant;
- reads → the last answer the cloud gave, saved in `/data`.

While local, the cloud is tried again every `cloud_retry_seconds`. In local mode the
Watts app sees no changes (the cloud receives nothing), but the thermostats, the central
unit and Home Assistant keep working.

**Local mode switch:** *Watts Central → Local mode (no cloud)* stops using the cloud at
runtime, without restarting the add-on (a restart would make the central unit look for
the secondary DNS). It is remembered across restarts. The `force_local` option does the
same at every start.

## Options

| Option | Default | Description |
|---|---|---|
| `language` | `en` | Entity names, schedule text and event details: `en` or `es`. Event types and states are always English. |
| `allow_control` | `false` | Create the climate and schedule entities. |
| `local_fallback` | `true` | Answer locally when the cloud fails. With `false`, the connection is closed. |
| `force_local` | `false` | Never use the cloud. |
| `mqtt_enabled` | `true` | Publish to Home Assistant. |
| `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password` | empty | Leave the host empty to use the Mosquitto add-on automatically. |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix. |
| `upstream_host` | `smarthome.wattselectronics.com` | Watts cloud. |
| `upstream_dns` | `1.1.1.1` | DNS used to find the real cloud (not the one doing the rewrite). |
| `upstream_ip_fallback` | `108.142.40.45` | Cloud address if that DNS query fails. |
| `listen_http` | `80` | Port of the proxy. The central unit always uses 80. |
| `cloud_timeout` | `5` | Seconds to wait for each cloud answer. |
| `cloud_retry_seconds` | `60` | Seconds between cloud retries while local. |
| `local_delay_ms` | `120` | Delay of local answers, to imitate the cloud. |
| `central_timeout_min` | `5` | See Health. |
| `zone_timeout_min` | `30` | See Health. |
| `timezone` | empty | Defaults to the time zone of Home Assistant. |
| `log_routine` | `false` | Log every request (very verbose). |
| `full_capture_connections` | `0` | Write the full content of the first N connections to `/share/watts_proxy`. |
| `max_payload_bytes` | `16384` | Maximum bytes captured per connection. |

## Logs and privacy

- Summary: the *Log* tab of the add-on.
- Detail: `/share/watts_proxy/YYYY-MM-DD.log`; events: `/share/watts_proxy/events.log`.

The logs contain the **MAC address of your central unit**, which is also its identifier
for the Watts cloud. Remove it before sharing logs in an issue.

## Protocol notes

What we know about the protocol of the BT-CT02-RF, for anyone who wants to build on it:

- Requests: `GET /api/v0.1/machine/<resource>/<action>/<URL-encoded JSON>` with numeric keys.
  Field `1` is `MAC#unix_time`, obfuscated by adding `ord("92533220362375"[i % 14])` to
  each character. The `100` field of the answers is the server time, obfuscated the same way.
- Answers: `{"code": "N", "data": {...}}`. `1` = OK/command, `2` = nothing pending, `8` = received.
- `device/edit` (one per thermostat): `2` id, `4` zone number, `6` weekly schedule (336
  chars, half hours, Monday first, `5` comfort, `0` eco), `7` comfort/manual setpoint,
  `8` frost setpoint, `9` eco setpoint, `10` boost setpoint, `14` boost seconds,
  `16` measured temperature, `22` mode, `33` heat demand. Temperatures are °F × 10
  even though the unit reports `C` (`716` = 22 °C).
- Modes (field `22`): `0` manual, `1` off, `2` frost protection, `3` eco, `4` boost,
  `8` program in a comfort period, `11` program in an eco period.
- A command is the answer to `query/check`:
  `{"code":"1","data":{"100":<time>,"2":"1","3":{"2":<thermostat id>, <fields>}}}`.
  Schedule commands carry only field `6`.

## Credits

- Fl0kkie, for the first fully local server for these central units and his notes in
  [pwesters/watts_vision#38](https://github.com/pwesters/watts_vision/issues/38).
- pwesters and RaginValdr, for the cloud integration (`watts_vision`) this work started from.

Not affiliated with or endorsed by Watts. "Watts" and "Watts Vision" are trademarks of
their owners.
