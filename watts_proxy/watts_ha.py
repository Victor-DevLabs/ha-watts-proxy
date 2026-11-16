"""Watts Proxy - Home Assistant side (MQTT).

Contains:
- a minimal MQTT 3.1.1 client on asyncio (CONNECT with credentials, QoS 0 PUBLISH,
  SUBSCRIBE, PINGREQ, retained "offline" will and automatic reconnection), no dependencies;
- the state of the central unit and its thermostats, built from what the central
  unit sends to the Watts cloud;
- MQTT discovery, which creates the devices and entities in Home Assistant;
- the event log (cloud commands, local commands, field changes) in events.log.

With allow_control, commands from Home Assistant are delivered by replacing a
"no pending commands" answer to query/check (from the cloud or, when the cloud is
down, from the proxy itself).
"""

import asyncio
import datetime as dt
import json
import os
import re
import struct

STATUS_TOPIC = "watts_proxy/status"
EVENTS_TOPIC = "watts_proxy/events"
EVENT_ENTITY_TOPIC = "watts_proxy/central/event"

# device/edit fields that are temperatures in °F x 10 (even though the unit reports "C").
TEMP_FIELDS = {"7", "8", "9", "10", "11", "12", "13", "16"}
FIELD_NAMES = {
    "6": "schedule", "7": "comfort setpoint", "8": "frost setpoint", "9": "eco setpoint",
    "10": "boost setpoint", "11": "field 11", "12": "minimum", "13": "maximum",
    "14": "boost time", "15": "requested mode", "16": "temperature", "17": "floor temperature",
    "22": "mode", "33": "demand",
}
# Thermostat mode (field 22). 8 and 11 are both "program": 8 while in a comfort period,
# 11 while in an eco period (confirmed on a BT-CT02-RF, firmware 03.63).
MODES = {"0": "manual", "1": "off", "2": "frost", "3": "eco", "4": "boost",
         "8": "program", "11": "program"}
PROGRAM_MODES = ("8", "11")
# Presets of the HA climate entity. "eco", "boost" and "away" are standard HA presets
# (translated in the UI, with icons); frost protection is published as "away".
PRESET_OF_MODE = {"3": "eco", "4": "boost", "2": "away"}
PRESETS = ["eco", "boost", "away"]
BOOST_DEFAULT_MIN = 120

# Health. Fields whose meaning is unknown and that have always been 0: candidates for
# low battery or a fault ("error_code" in the cloud API; nobody knows the local field yet).
WATCH_FIELDS = ("23", "34", "35")
KNOWN_FIELDS = {"1", "2", "3", "4", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15",
                "16", "17", "22", "23", "33", "34", "35"}
EVENT_TYPES = ["command_confirmed", "command_failed", "schedule_rejected", "cloud_unavailable",
               "cloud_restored", "central_no_data", "central_restored", "thermostat_warning"]

# --------------------------------------------------------------------------- texts

# Only what the user sees in Home Assistant is translated (entity names, schedule text,
# event details). Logs, MQTT payload keys, states and event types are always English.
TEXTS = {
    "en": {
        "central": "Watts Central", "zone": "Zone {n}", "thermostat_model": "Thermostat {dev}",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "all_eco": "all eco", "level": "level {c}",
        "central_restored": "The central unit is sending data again",
        "central_no_data": "The central unit has sent no data for {m} min",
        "no_reports": "no reports for {m} min",
        "field": "field {k} = {v}", "unknown_fields": "{fields} (unidentified: low battery or fault?)",
        "health_no_data": "The central unit is not sending data",
        "health_cloud": "Cloud unavailable: the proxy answers locally ({reason})",
        "health_failed": "Command failed: {text}",
        "cloud_unavailable": "The proxy answers the central unit locally ({reason})",
        "cloud_restored": "Forwarding to the cloud again",
        "not_confirmed": "The central unit did not confirm: {label}",
        "cmd_manual": "manual at {t} °C", "cmd_eco": "eco at {t} °C",
        "cmd_frost": "frost protection at {t} °C", "cmd_boost": "boost at {t} °C for {m} min",
        "cmd_off": "off", "cmd_program": "program", "cmd_schedule": "schedule {text}",
    },
    "es": {
        "central": "Watts central", "zone": "Zona {n}", "thermostat_model": "Termostato {dev}",
        "days": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
        "all_eco": "todo eco", "level": "nivel {c}",
        "central_restored": "La central vuelve a enviar datos",
        "central_no_data": "La central no envía datos desde hace {m} min",
        "no_reports": "sin informes desde hace {m} min",
        "field": "campo {k} = {v}", "unknown_fields": "{fields} (sin identificar: ¿batería baja o avería?)",
        "health_no_data": "La central no envía datos",
        "health_cloud": "Nube no disponible: el proxy responde en local ({reason})",
        "health_failed": "Orden fallida: {text}",
        "cloud_unavailable": "El proxy responde a la central en local ({reason})",
        "cloud_restored": "Vuelve el reenvío a la nube",
        "not_confirmed": "La central no confirmó: {label}",
        "cmd_manual": "manual a {t} °C", "cmd_eco": "eco a {t} °C",
        "cmd_frost": "antihielo a {t} °C", "cmd_boost": "boost a {t} °C durante {m} min",
        "cmd_off": "apagado", "cmd_program": "programa", "cmd_schedule": "horario {text}",
    },
}

# Entity names: key -> (English, Spanish).
NAMES = {
    "temperature": ("Temperature", "Temperatura"),
    "setpoint": ("Setpoint", "Consigna"),
    "comfort_setpoint": ("Comfort setpoint", "Consigna confort"),
    "eco_setpoint": ("Eco setpoint", "Consigna eco"),
    "field_11": ("Field 11", "Campo 11"),
    "boost_setpoint": ("Boost setpoint", "Consigna boost"),
    "frost_setpoint": ("Frost protection setpoint", "Consigna antihielo"),
    "boost_remaining": ("Boost remaining", "Boost restante"),
    "period": ("Program period", "Tramo del programa"),
    "next_change": ("Next change", "Próximo cambio"),
    "mode": ("Mode", "Modo"),
    "heat_demand": ("Heat demand", "Demanda de calor"),
    "warning": ("Warning", "Aviso"),
    "last_report": ("Last report", "Último informe"),
    "field_14": ("Field 14", "Campo 14"),
    "schedule": ("Schedule", "Horario"),
    "any_demand": ("Heat demand (any zone)", "Demanda de calor (alguna zona)"),
    "zones_with_demand": ("Zones with demand", "Zonas con demanda"),
    "connection": ("Connection", "Conexión"),
    "force_local": ("Local mode (no cloud)", "Modo local (sin nube)"),
    "boost_duration": ("Boost duration", "Duración del boost"),
    "last_data": ("Last data", "Último dato"),
    "communication": ("Communication", "Comunicación"),
    "status": ("Status", "Estado"),
    "events": ("Events", "Eventos"),
}


# --------------------------------------------------------------------------- pure logic

def climate_view(f):
    """(hvac_mode, preset, hvac_action) of the HA climate entity from the thermostat fields."""
    mode = str(f.get("22", ""))
    if mode == "1":
        return "off", "none", "off"
    hvac = "auto" if mode in PROGRAM_MODES else "heat"
    action = "heating" if str(f.get("33")) == "1" else "idle"
    return hvac, PRESET_OF_MODE.get(mode, "none"), action


def program_period(schedule, when=None):
    """Current period of the weekly schedule (336 chars, Monday first, half hours): 'comfort' or 'eco'."""
    if not isinstance(schedule, str):
        return None
    when = when or dt.datetime.now()
    if len(schedule) >= 336:
        c = schedule[when.weekday() * 48 + when.hour * 2 + when.minute // 30]
    elif len(schedule) >= 168:          # in case some firmware sends it per hour
        c = schedule[when.weekday() * 24 + when.hour]
    else:
        return None
    return "eco" if c == "0" else "comfort"


def _hhmm(slot):
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}" if slot < 48 else "24:00"


def _day_label(days, names):
    """[0, 1, 2, 4] -> 'Mon–Wed, Fri'; [5, 6] -> 'Sat–Sun'; [0, 2] -> 'Mon, Wed'."""
    parts, i = [], 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and days[j + 1] == days[j] + 1:
            j += 1
        if j - i >= 2 or (j - i == 1 and days[i] == 5):
            parts.append(f"{names[days[i]]}–{names[days[j]]}")
        else:
            parts.extend(names[d] for d in days[i:j + 1])
        i = j + 1
    return ", ".join(parts)


def schedule_summary(schedule, lang="en"):
    """Schedule (336 chars) -> ["Mon, Wed: 05:30–09:30, 13:00–15:30", ...] in week order.

    Lists the periods that are not eco ("0"). Comfort ("5") has no mark; any other value
    is marked "[level X]". Days with the same schedule are grouped, even if not consecutive.
    """
    if not isinstance(schedule, str) or len(schedule) < 336:
        return []
    tx = TEXTS.get(lang, TEXTS["en"])
    texts = []
    for d in range(7):
        day, runs, i = schedule[d * 48:(d + 1) * 48], [], 0
        while i < 48:
            c = day[i]
            if c != "0":
                j = i
                while j < 48 and day[j] == c:
                    j += 1
                runs.append(f"{_hhmm(i)}–{_hhmm(j)}" + ("" if c == "5" else f" [{tx['level'].format(c=c)}]"))
                i = j
            else:
                i += 1
        texts.append(", ".join(runs) if runs else tx["all_eco"])
    groups = {}
    for d, t in enumerate(texts):
        groups.setdefault(t, []).append(d)
    return [f"{_day_label(days, tx['days'])}: {t}"
            for t, days in sorted(groups.items(), key=lambda kv: kv[1][0])]


def schedule_text(schedule, lang="en"):
    """Schedule -> editable text ('Mon–Fri: 06:00–15:30; Sat–Sun: all eco')."""
    return "; ".join(schedule_summary(schedule, lang))


# Day names accepted when parsing (English and Spanish, without accents).
_DAY_KEYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
_ALL_DAYS = ("all", "every day", "week", "todos", "todos los dias", "semana")
_ALL_ECO = ("all eco", "todo eco", "eco", "-", "")
_ALL_COMFORT = ("all comfort", "todo confort", "comfort", "confort")


def _norm(s):
    return (s.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o")
            .replace("ú", "u").replace("–", "-").replace("—", "-").strip())


def _parse_days(text):
    """'Mon–Fri, Sun' (or 'Lun–Vie, Dom') -> [0, 1, 2, 3, 4, 6]. 'all' -> the 7 days."""
    t = _norm(text)
    if t in _ALL_DAYS:
        return list(range(7))
    days = []
    for part in t.split(","):
        part = part.strip()
        if not part:
            continue
        ends = [p.strip()[:3] for p in part.split("-")]
        if len(ends) > 2 or any(e not in _DAY_KEYS for e in ends):
            raise ValueError(f"invalid day: '{part.strip()}' (use Mon, Tue, Wed, Thu, Fri, Sat, Sun)")
        a, b = _DAY_KEYS[ends[0]], _DAY_KEYS[ends[-1]]
        if b < a:
            raise ValueError(f"day range backwards: '{part.strip()}'")
        days.extend(range(a, b + 1))
    if not days:
        raise ValueError(f"missing days in '{text.strip()}'")
    return sorted(set(days))


def _parse_time(t):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", t.strip())
    if not m:
        raise ValueError(f"invalid time: '{t.strip()}'")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if mi not in (0, 30) or h > 24 or (h == 24 and mi):
        raise ValueError(f"'{t.strip()}': only full or half hours (00:00–24:00)")
    return h * 2 + mi // 30


def parse_schedule(text, current=None):
    """Schedule text -> 336-char string ("5" comfort, "0" eco).

    Format (the same one Home Assistant shows): groups separated by ';' or new lines,
    each '<days>: <comfort periods>'. Examples:
        'Mon–Fri: 06:00–09:30, 13:00–15:30; Sat–Sun: all eco'
        'Sat: 09:00–13:00'            (changes Saturday only)
    Spanish day names are accepted too. Days not mentioned keep their current schedule
    (or are eco if there is none). The raw 336 characters of 0 and 5 are accepted as well.
    """
    raw = (text or "").strip()
    if re.fullmatch(r"[05]{336}", raw):
        return raw
    base = current if isinstance(current, str) and len(current) >= 336 else "0" * 336
    days_out = [list(base[d * 48:(d + 1) * 48]) for d in range(7)]
    groups = [g for g in re.split(r"[;\n]", raw) if g.strip()]
    if not groups:
        raise ValueError("empty schedule")
    seen = set()
    for g in groups:
        m = re.fullmatch(r"\s*([^:\d]+?)\s*:\s*(.*?)\s*", g)
        if not m:
            raise ValueError(f"expected '<days>: <periods>' in '{g.strip()}'")
        days = _parse_days(m.group(1))
        if seen & set(days):
            raise ValueError(f"repeated day in '{g.strip()}'")
        seen |= set(days)
        body = re.sub(r"\s*-\s*", "-", _norm(m.group(2)))     # "06:00 – 09:30" -> "06:00-09:30"
        slots = ["0"] * 48
        if body in _ALL_ECO:
            pass
        elif body in _ALL_COMFORT:
            slots = ["5"] * 48
        else:
            for run in [r for r in re.split(r"[,\s]+(?=\d)", body) if r.strip()]:
                parts = run.split("-")
                if len(parts) != 2:
                    raise ValueError(f"invalid period: '{run.strip()}' (use 06:00–09:30)")
                a, b = _parse_time(parts[0]), _parse_time(parts[1])
                if a >= b:
                    raise ValueError(f"period '{run.strip()}': start must be before end")
                for i in range(a, b):
                    slots[i] = "5"
        for d in days:
            days_out[d] = slots
    return "".join("".join(d) for d in days_out)


def next_change(schedule, when=None):
    """Date and time (timezone-aware) of the next comfort/eco change, or None if it never changes."""
    if not isinstance(schedule, str) or len(schedule) < 336:
        return None
    when = (when or dt.datetime.now()).astimezone()
    cur = when.weekday() * 48 + when.hour * 2 + when.minute // 30
    is_eco = schedule[cur] == "0"
    for k in range(1, 337):
        if (schedule[(cur + k) % 336] == "0") != is_eco:
            start = when.replace(minute=30 * (when.minute // 30), second=0, microsecond=0)
            return start + dt.timedelta(minutes=30 * k)
    return None


def active_setpoint(f, when=None):
    """Setpoint the thermostat is applying, in °C, and the period when in program mode.

    comfort/manual -> 7, eco -> 9, boost -> 10, frost -> 8, program -> 7 or 9 depending on
    the schedule (field 6). Field 11 is not reliable: it can keep an old value.
    """
    mode = str(f.get("22", ""))
    if mode in PROGRAM_MODES:
        period = program_period(f.get("6"), when)
        if period:
            return f10_to_c(f.get("7" if period == "comfort" else "9")), period
        return f10_to_c(f.get("11")), None
    if mode == "1":
        return None, None
    key = {"0": "7", "3": "9", "4": "10", "2": "8"}.get(mode)
    return f10_to_c(f.get(key or "11")), None


def f10_to_c(v):
    """'716' (71.6 °F) -> 22.0 °C. None if it is not a number."""
    try:
        return round((int(v) / 10 - 32) * 5 / 9, 1)
    except (TypeError, ValueError):
        return None


def c_to_f10(c):
    """22.0 °C -> '716' (°F x 10), rounding to 0.5 °C and limiting to 5-30 °C."""
    c = min(30.0, max(5.0, round(float(c) * 2) / 2))
    return str(int(round(round(c * 9 / 5 + 32, 1) * 10)))


def show(key, value):
    if key in TEMP_FIELDS:
        c = f10_to_c(value)
        return f"{value} ({c} °C)" if c is not None else str(value)
    if key in ("22", "15"):
        return f"{value} ({MODES.get(str(value), '?')})"
    return str(value)


def data_file(data_dir, name, old_name=None):
    """Path of a file in /data. Renames the file used by versions before 0.9.0 if needed."""
    path = os.path.join(data_dir, name)
    if old_name:
        old = os.path.join(data_dir, old_name)
        if not os.path.exists(path) and os.path.exists(old):
            try:
                os.replace(old, path)
            except OSError:
                return old
    return path


# --------------------------------------------------------------------------- MQTT

def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _remaining(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


class MiniMqtt:
    """MQTT 3.1.1 client, just enough to publish retained states and receive commands."""

    def __init__(self, host, port, user, password, client_id, log):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.client_id = client_id
        self.log = log
        self.writer = None
        self.connected = asyncio.Event()
        self.retained = {}          # topic -> payload, sent again after reconnecting
        self.subs = {}              # topic (or pattern with + / #) -> callback(topic, payload)
        self._pid = 0

    def subscribe(self, topic, callback):
        self.subs[topic] = callback
        if self.connected.is_set():
            self._send_subscribe(topic)

    def _send_subscribe(self, topic):
        self._pid = self._pid % 65535 + 1
        body = struct.pack(">H", self._pid) + _str(topic) + b"\x00"
        self.writer.write(b"\x82" + _remaining(len(body)) + body)

    @staticmethod
    def _match(pattern, topic):
        p, t = pattern.split("/"), topic.split("/")
        for i, part in enumerate(p):
            if part == "#":
                return True
            if i >= len(t) or (part != "+" and part != t[i]):
                return False
        return len(p) == len(t)

    def _dispatch(self, topic, payload):
        for pattern, cb in self.subs.items():
            if self._match(pattern, topic):
                try:
                    cb(topic, payload)
                except Exception as e:  # noqa: BLE001
                    self.log(f"[mqtt] error handling {topic}: {type(e).__name__}: {e}")

    def start(self):
        asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        delay = 5
        while True:
            try:
                await self._session()
                delay = 5
            except Exception as e:  # noqa: BLE001
                self.log(f"[mqtt] disconnected from {self.host}:{self.port}: {type(e).__name__}: {e}")
            self.connected.clear()
            self.writer = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def _session(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), 10)
        flags = 0x02 | 0x04 | 0x20                # clean session + retained will
        payload = _str(self.client_id) + _str(STATUS_TOPIC) + _str("offline")
        if self.user:
            flags |= 0x80
            payload += _str(self.user)
            if self.password:
                flags |= 0x40
                payload += _str(self.password)
        var = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
        body = var + payload
        writer.write(b"\x10" + _remaining(len(body)) + body)
        await writer.drain()
        hdr = await asyncio.wait_for(reader.readexactly(4), 10)
        if hdr[0] != 0x20 or hdr[3] != 0:
            codes = {1: "protocol version", 2: "client id", 3: "server unavailable",
                     4: "bad user name or password", 5: "not authorized"}
            writer.close()
            raise RuntimeError(f"CONNACK refused: {codes.get(hdr[3], hdr[3])}")
        self.writer = writer
        self.connected.set()
        self.log(f"[mqtt] connected to {self.host}:{self.port} as {self.user or '(anonymous)'}")
        self._send(STATUS_TOPIC, "online", True)
        for topic, payload in list(self.retained.items()):
            self._send(topic, payload, True)
        for topic in list(self.subs):
            self._send_subscribe(topic)
        await writer.drain()

        async def pinger():
            while True:
                await asyncio.sleep(30)
                writer.write(b"\xc0\x00")
                await writer.drain()

        ping = asyncio.get_running_loop().create_task(pinger())
        try:
            while True:                       # incoming PUBLISH; anything else is ignored
                first = (await reader.readexactly(1))[0]
                mult, length = 1, 0
                while True:
                    b = (await reader.readexactly(1))[0]
                    length += (b & 0x7F) * mult
                    mult *= 128
                    if not b & 0x80:
                        break
                body = await reader.readexactly(length) if length else b""
                if first & 0xF0 == 0x30 and len(body) >= 2:
                    tl = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tl].decode("utf-8", "replace")
                    start = 2 + tl + (2 if (first >> 1) & 0x03 else 0)   # packet id if QoS > 0
                    self._dispatch(topic, body[start:].decode("utf-8", "replace"))
        finally:
            ping.cancel()
            writer.close()

    def _send(self, topic, payload, retain):
        if not self.writer:
            return
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        body = _str(topic) + data
        self.writer.write(bytes([0x30 | (1 if retain else 0)]) + _remaining(len(body)) + body)

    def publish(self, topic, payload, retain=False):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        if retain:
            self.retained[topic] = payload
        if self.connected.is_set():
            try:
                self._send(topic, payload, retain)
            except (OSError, RuntimeError) as e:
                self.log(f"[mqtt] error publishing {topic}: {e}")


# --------------------------------------------------------------------------- state and HA

class WattsState:
    def __init__(self, opt, log, log_dir, data_dir=None):
        self.opt, self.log, self.log_dir = opt, log, log_dir
        data_dir = data_dir or os.environ.get("WP_DATA", "/data")
        self.lang = opt.get("language") if opt.get("language") in TEXTS else "en"
        self.prefix = opt.get("discovery_prefix") or "homeassistant"
        self.mac = None
        self.central = {}
        self.devices = {}          # "C004-003" -> last device/edit (merged)
        self.zones = {}            # "4" -> zone name sent by the central unit
        self.announced = {}        # "C004-003" -> name it was announced with
        self.central_announced = False
        self.allow_control = bool(opt.get("allow_control", False))
        self.queue = {}            # "<dev>|<kind>" -> command waiting to be sent (last one wins)
        self.pending = {}          # "<dev>|<kind>" -> command sent, waiting for confirmation
        self.slugs = {}            # "c004_003" -> "C004-003"
        self.connection = "cloud"  # "cloud" or "local" (the proxy answers instead of the cloud)
        self.connection_reason = ""
        self.force_local = False   # "Local mode" switch in HA
        self.force_local_handler = None   # set by proxy.py: function(bool, source)
        self.last_data = None      # time of the last message from the central unit

        def load(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return {}

        self.schedules_file = data_file(data_dir, "schedules.json", "horarios.json")
        self.schedules = load(self.schedules_file)          # "C004-003" -> schedule (field 6)
        self.zones_file = data_file(data_dir, "zones.json", "zonas.json")
        self.zones.update(load(self.zones_file))
        self.settings_file = data_file(data_dir, "settings.json", "ajustes.json")
        self.settings = load(self.settings_file)
        if "boost_minutos" in self.settings:                 # name before 0.9.0
            self.settings.setdefault("boost_minutes", self.settings.pop("boost_minutos"))
        self.settings.setdefault("boost_minutes", BOOST_DEFAULT_MIN)
        # ---- health
        self.started = dt.datetime.now(dt.timezone.utc)
        self.central_timeout = max(1, int(opt.get("central_timeout_min", 5))) * 60
        self.zone_timeout = max(5, int(opt.get("zone_timeout_min", 30))) * 60
        self.dev_seen = {}          # "C004-003" -> time of the last device/edit
        self.zone_alerts = {}       # "C004-003" -> {reason key: text}
        self.central_ok = None      # True/False depending on recent data
        self.recent_failures = []   # [(time, text)] failed commands (forgotten after an hour)
        self.mqtt = None
        if opt.get("mqtt_enabled", True):
            self.mqtt = MiniMqtt(opt.get("mqtt_host") or "127.0.0.1", int(opt.get("mqtt_port") or 1883),
                                 opt.get("mqtt_user") or "", opt.get("mqtt_password") or "",
                                 "watts_proxy", log)

    def t(self, key, **kw):
        return TEXTS[self.lang][key].format(**kw)

    def name(self, key):
        return NAMES[key][1 if self.lang == "es" else 0]

    def start(self):
        if self.mqtt:
            if self.allow_control:
                self.mqtt.subscribe("watts_proxy/+/set/+", self._on_command)
                self.log("[control] control from Home Assistant ENABLED (allow_control: true)")
            else:
                self.log("[control] control from Home Assistant disabled: read only")
            # Central settings (local mode, boost duration) work even without allow_control.
            self.mqtt.subscribe("watts_proxy/central/set/+", self._on_central)
            self.mqtt.start()
            asyncio.get_running_loop().create_task(self._republish_loop())
            asyncio.get_running_loop().create_task(self._health_loop())
        else:
            self.log("[mqtt] disabled (mqtt_enabled: false)")

    def _save_json(self, path, obj):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            self.log(f"[ha] could not save {path}: {e}")

    async def _republish_loop(self):
        """Every minute, recompute the setpoint of zones in program mode (period changes)."""
        last = {}
        while True:
            await asyncio.sleep(60)
            for dev, f in list(self.devices.items()):
                if str(f.get("22")) not in PROGRAM_MODES:
                    continue
                cur = active_setpoint(f)
                if last.get(dev) not in (None, cur):
                    try:
                        self._publish_device(dev)
                    except Exception as e:  # noqa: BLE001
                        self.log(f"[ha] error republishing {dev}: {e}")
                last[dev] = cur

    # ---- health

    async def _health_loop(self):
        """Every 30 s: communication with the central unit, silent zones and overall status."""
        while True:
            await asyncio.sleep(30)
            try:
                self.check_health()
            except Exception as e:  # noqa: BLE001
                self.log(f"[health] error: {type(e).__name__}: {e}")

    def check_health(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        ref = self.last_data or self.started
        ok = (now - ref).total_seconds() < self.central_timeout
        if ok != self.central_ok:
            if self.central_ok is not None or not ok:
                if ok:
                    self.ha_event("central_restored", detail=self.t("central_restored"))
                else:
                    mins = int((now - ref).total_seconds() // 60)
                    self.event(f"CENTRAL UNIT SILENT for {mins} min")
                    self.ha_event("central_no_data", detail=self.t("central_no_data", m=mins))
            self.central_ok = ok
            self._publish_central()
        # Zones that stop reporting (only while the central unit does report; otherwise the
        # warning belongs to the central unit).
        for dev in list(self.devices):
            seen = self.dev_seen.get(dev)
            stale = ok and seen is not None and (now - seen).total_seconds() > self.zone_timeout
            self._set_zone_reason(dev, "no_reports", stale,
                                  self.t("no_reports", m=int((now - seen).total_seconds() // 60)) if stale else "")
        cutoff = now - dt.timedelta(hours=1)
        before = len(self.recent_failures)
        self.recent_failures = [x for x in self.recent_failures if x[0] > cutoff]
        if len(self.recent_failures) != before:
            self._publish_central()

    def _set_zone_reason(self, dev, key, active, text):
        """Set or clear a warning reason of a zone; publish and notify only on change."""
        reasons = self.zone_alerts.setdefault(dev, {})
        if active and reasons.get(key) == text:
            return
        if not active and key not in reasons:
            return
        if active:
            new = key not in reasons
            reasons[key] = text
            if new:
                self.event(f"WARNING {dev} ({self._zone_name(dev)}): {text}")
                self.ha_event("thermostat_warning", thermostat=dev, zone=self._zone_name(dev), detail=text)
        else:
            del reasons[key]
            self.event(f"WARNING {dev} ({self._zone_name(dev)}) cleared: {key}")
        if self.mqtt and dev in self.devices:
            self._publish_device(dev)

    def _check_fields(self, dev, fields):
        """Unidentified fields (low battery or fault candidates) or new fields."""
        odd = {k: v for k, v in fields.items()
               if (k in WATCH_FIELDS and str(v) not in ("0", "")) or
               (k not in KNOWN_FIELDS and not k.startswith("_"))}
        text = self.t("unknown_fields", fields=", ".join(
            self.t("field", k=k, v=v) for k, v in sorted(odd.items()))) if odd else ""
        self._set_zone_reason(dev, "fields", bool(odd), text)

    def health(self):
        """(status, problems) for the Status entity of the central unit."""
        problems, warnings = [], []
        if self.central_ok is False:
            problems.append(self.t("health_no_data"))
        if self.connection == "local" and not self.force_local:
            warnings.append(self.t("health_cloud", reason=self.connection_reason))
        for dev, reasons in self.zone_alerts.items():
            for text in reasons.values():
                warnings.append(f"{self._zone_name(dev)}: {text}")
        for _, text in self.recent_failures:
            warnings.append(self.t("health_failed", text=text))
        status = "problem" if problems else "warning" if warnings else "ok"
        return status, problems + warnings

    def ha_event(self, event_type, **attrs):
        """Event for the Events entity in HA (automations). Not retained."""
        if self.mqtt:
            self.mqtt.publish(EVENT_ENTITY_TOPIC, {"event_type": event_type, **attrs})

    # ---- control from HA

    def set_force_local(self, on: bool):
        """State and publishing only; the cloud/local logic lives in proxy.py."""
        if on != self.force_local:
            self.force_local = on
            self.event("FORCED LOCAL MODE on: the cloud is not used" if on
                       else "FORCED LOCAL MODE off: using the cloud again",
                       {"type": "force_local", "state": "on" if on else "off"})
        self._publish_central()

    def _on_force_local(self, payload):
        on = payload.strip().upper() in ("ON", "1", "TRUE")
        if self.force_local_handler:
            self.force_local_handler(on, "HA switch")
        else:
            self.set_force_local(on)

    def _on_boost_minutes(self, payload):
        try:
            minutes = int(float(payload))
        except ValueError:
            self.log(f"[control] invalid boost duration: {payload!r}")
            return
        minutes = min(240, max(5, minutes))
        if minutes != self.settings.get("boost_minutes"):
            self.settings["boost_minutes"] = minutes
            self._save_json(self.settings_file, self.settings)
            self.log(f"[control] boost duration: {minutes} min")
        self._publish_central()

    # Commands use the same format as the Watts app (captured traffic) and Fl0kkie's server.
    # Field 14 = boost seconds (0 outside boost); 22 = mode; 15 = requested mode.

    def _cmd_manual(self, dev, v):
        return ({"2": dev, "14": "0", "7": v, "11": v, "22": "0", "15": "0"},
                {"7": v, "22": "0"}, self.t("cmd_manual", t=f10_to_c(v)))

    def _cmd_eco(self, dev, v):
        return ({"2": dev, "14": "0", "9": v, "11": v, "22": "3", "15": "3"},
                {"9": v, "22": "3"}, self.t("cmd_eco", t=f10_to_c(v)))

    def _cmd_frost(self, dev, v):
        return ({"2": dev, "14": "0", "8": v, "11": v, "22": "2", "15": "2"},
                {"8": v, "22": "2"}, self.t("cmd_frost", t=f10_to_c(v)))

    def _cmd_boost(self, dev, v, seconds):
        return ({"2": dev, "14": str(seconds), "10": v, "11": v, "22": "4", "15": "4"},
                {"10": v, "22": "4"}, self.t("cmd_boost", t=f10_to_c(v), m=seconds // 60))

    def _cmd_off(self, dev):
        return ({"2": dev, "14": "0", "11": "0", "22": "1", "15": "1"}, {"22": "1"}, self.t("cmd_off"))

    def _cmd_program(self, dev, cur):
        # The app also sends field 11 = the eco setpoint of the zone (field 9).
        cmd = {"2": dev, "14": "0", "22": "11", "15": "11"}
        if cur.get("9"):
            cmd["11"] = cur["9"]
        return cmd, {"22": PROGRAM_MODES}, self.t("cmd_program")

    def _on_central(self, topic, payload):
        what = topic.split("/")[3]               # watts_proxy/central/set/<what>
        if what == "force_local":
            self._on_force_local(payload)
        elif what in ("boost_minutes", "boost_minutos"):         # "boost_minutos": before 0.9.0
            self._on_boost_minutes(payload)

    def _on_command(self, topic, payload):
        parts = topic.split("/")                 # watts_proxy/<slug>/set/<what>
        what = parts[3]
        if parts[1] == "central":
            return                               # handled by _on_central
        dev = self.slugs.get(parts[1])
        if not dev or dev not in self.devices:
            self.log(f"[control] command for an unknown thermostat: {topic}")
            return
        # State "as HA sees it": includes a command in progress, to chain quick changes.
        cur = {**self.devices[dev], **self._overlay(dev)}
        mode = str(cur.get("22", ""))
        payload = payload.strip()
        boost_s = int(self.settings.get("boost_minutes", BOOST_DEFAULT_MIN)) * 60
        if what in ("schedule", "horario"):                    # "horario": before 0.9.0
            self._on_schedule(dev, cur, payload)
            return
        if what == "temperature":
            # The setpoint that changes is the one of the current mode, as on the thermostat.
            v = c_to_f10(payload)
            if mode == "3":
                order = self._cmd_eco(dev, v)
            elif mode == "4":
                left = int(cur.get("14") or 0)
                order = self._cmd_boost(dev, v, left if left > 0 else boost_s)
            elif mode == "2":
                order = self._cmd_frost(dev, c_to_f10(min(10.0, float(payload))))
            else:                                # manual, program or off -> manual
                order = self._cmd_manual(dev, v)
        elif what == "mode" and payload == "heat":
            order = self._cmd_manual(dev, cur.get("7") or c_to_f10(20))
        elif what == "mode" and payload == "auto":
            order = self._cmd_program(dev, cur)
        elif what == "mode" and payload == "off":
            order = self._cmd_off(dev)
        elif what == "preset" and payload == "eco":
            order = self._cmd_eco(dev, cur.get("9") or c_to_f10(20))
        elif what == "preset" and payload == "boost":
            order = self._cmd_boost(dev, cur.get("10") or cur.get("7") or c_to_f10(22), boost_s)
            if mode == "1":
                # Tested on a real thermostat: from off, the central unit accepts the boost
                # but the thermostat does not apply it (and the central unit never corrects
                # itself). So: manual first and, once confirmed, the boost.
                first = self._cmd_manual(dev, cur.get("7") or c_to_f10(20))
                self._enqueue(dev, *first, then=order)
                return
        elif what == "preset" and payload in ("away", "antihielo"):   # "antihielo": before 0.7.2
            order = self._cmd_frost(dev, cur.get("8") or c_to_f10(7))
        elif what == "preset" and payload == "none":
            order = self._cmd_manual(dev, cur.get("7") or c_to_f10(20))
        else:
            self.log(f"[control] unsupported command: {what}={payload!r}")
            return
        self._enqueue(dev, *order)

    def _on_schedule(self, dev, cur, payload):
        """Schedule change from HA: fully validated before anything is sent."""
        old = cur.get("6")
        try:
            new = parse_schedule(payload, old)
        except ValueError as e:
            self.event(f"SCHEDULE REJECTED for {dev}: {e}",
                       {"type": "schedule", "state": "rejected", "thermostat": dev, "reason": str(e)})
            self.ha_event("schedule_rejected", thermostat=dev, zone=self._zone_name(dev), detail=str(e))
            self._publish_device(dev)            # the text entity goes back to the real schedule
            return
        if new == old:
            self.log(f"[control] {dev}: the requested schedule equals the current one")
            self._publish_device(dev)
            return
        # The previous schedule goes to events.log, so it can be restored by copying it.
        self.event(f"{dev}: previous schedule: {schedule_text(old) or '(unknown)'} | {old or ''}")
        self._enqueue(dev, {"2": dev, "6": new}, {"6": new},
                      self.t("cmd_schedule", text=schedule_text(new, self.lang)), kind="schedule")

    # Each thermostat has two independent queue slots: "mode" (mode and setpoints) and
    # "schedule" (field 6), as in Fl0kkie's server. In each slot, the last command wins.
    KINDS = ("mode", "schedule")

    @staticmethod
    def _key(dev, kind):
        return f"{dev}|{kind}"

    def _overlay(self, dev):
        """Fields of the commands in progress (queued or unconfirmed), to show them in HA at once."""
        out = {}
        for kind in self.KINDS:
            k = self._key(dev, kind)
            item = self.queue.get(k) or self.pending.get(k)
            if item:
                # In a chain (off -> manual -> boost) the final result is shown already.
                final = item["then"][0] if item.get("then") else item["cmd"]
                out.update({f: v for f, v in final.items() if f not in ("2", "15")})
        return out

    def _enqueue(self, dev, cmd, expect, label, then=None, kind="mode"):
        """Queue a command. `then` = (cmd, expect, label) to send once this one is confirmed."""
        key = self._key(dev, kind)
        self.pending.pop(key, None)
        self.queue[key] = {"dev": dev, "kind": kind, "cmd": cmd, "expect": expect, "label": label,
                           "attempts": 0, "then": then}
        if then:
            label = f"{label} -> {then[2]}"
        self.event(f"LOCAL COMMAND queued for {dev}: {label}",
                   {"type": "local_command", "state": "queued", "thermostat": dev, "fields": cmd})
        self._publish_device(dev)                # HA shows the change without waiting

    def _due_retries(self):
        now = asyncio.get_running_loop().time()
        for key, item in list(self.pending.items()):
            if now - item["sent"] < 45:
                continue
            dev = item["dev"]
            del self.pending[key]
            if item["attempts"] < 3 and key not in self.queue:
                self.queue[key] = item
                self.log(f"[control] {dev}: not confirmed, retry {item['attempts'] + 1}/3")
            else:
                self.event(f"LOCAL COMMAND FAILED for {dev}: {item['label']} "
                           f"(not confirmed after {item['attempts']} attempts)",
                           {"type": "local_command", "state": "failed", "thermostat": dev,
                            "fields": item["cmd"]})
                text = f"{self._zone_name(dev)}: {item['label']}"
                self.recent_failures.append((dt.datetime.now(dt.timezone.utc), text))
                self.ha_event("command_failed", thermostat=dev, zone=self._zone_name(dev),
                              detail=self.t("not_confirmed", label=item["label"]))
                try:
                    self._publish_device(dev)    # HA shows the real state again
                except Exception as e:  # noqa: BLE001
                    self.log(f"[ha] error republishing {dev}: {e}")

    def transform_response(self, msg: bytes, last_req):
        """Replace a 'no pending commands' answer to query/check with a local command."""
        if not self.allow_control or last_req != "query/check":
            return msg
        self._due_retries()
        if not self.queue:
            return msg
        head, sep, body = msg.partition(b"\r\n\r\n")
        try:
            j = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return msg
        if not isinstance(j, dict) or j.get("code") != "2" or "100" not in (j.get("data") or {}):
            return msg                           # the cloud has something: leave it alone
        key = next(iter(self.queue))
        item = self.queue.pop(key)
        dev = item["dev"]
        new_body = json.dumps({"code": "1", "data": {"100": j["data"]["100"], "2": "1", "3": item["cmd"]}},
                              separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        lines = [ln for ln in head.split(b"\r\n") if not ln.lower().startswith(b"content-length:")]
        lines.append(b"Content-Length: " + str(len(new_body)).encode())
        item["attempts"] += 1
        item["sent"] = asyncio.get_running_loop().time()
        self.pending[key] = item
        self.event(f"LOCAL COMMAND sent to {dev}: {item['label']} (attempt {item['attempts']})",
                   {"type": "local_command", "state": "sent", "thermostat": dev, "fields": item["cmd"]})
        return b"\r\n".join(lines) + sep + new_body

    def _check_confirmation(self, dev, fields):
        for kind in self.KINDS:
            key = self._key(dev, kind)
            item = self.pending.get(key)
            if item and all(str(fields.get(k)) in (v if isinstance(v, tuple) else (v,))
                            for k, v in item["expect"].items()):
                del self.pending[key]
                self.event(f"LOCAL COMMAND confirmed by {dev}: {item['label']}",
                           {"type": "local_command", "state": "confirmed", "thermostat": dev})
                self.ha_event("command_confirmed", thermostat=dev, zone=self._zone_name(dev),
                              detail=item["label"])
                if item.get("then") and key not in self.queue:
                    self._enqueue(dev, *item["then"], kind=kind)

    # ---- connection with the cloud

    def set_connection(self, local: bool, reason=""):
        state = "local" if local else "cloud"
        if state == self.connection:
            return
        # Forced local mode (HA switch) is not an outage: no events for HA.
        forced = "forced" in (reason if local else self.connection_reason)
        self.connection, self.connection_reason = state, reason
        if local:
            self.event(f"CLOUD UNAVAILABLE: the proxy answers the central unit locally ({reason})",
                       {"type": "connection", "state": "local", "reason": reason})
            if not forced:
                self.ha_event("cloud_unavailable", detail=self.t("cloud_unavailable", reason=reason))
        else:
            self.event("CLOUD RESTORED: forwarding to the cloud again",
                       {"type": "connection", "state": "cloud"})
            if not forced:
                self.ha_event("cloud_restored", detail=self.t("cloud_restored"))
        self._publish_central()

    # ---- events

    def event(self, text, payload=None):
        self.log(f"[event] {text}")
        try:
            with open(os.path.join(self.log_dir, "events.log"), "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError as e:
            self.log(f"[event] could not write events.log: {e}")
        if self.mqtt and payload is not None:
            self.mqtt.publish(EVENTS_TOPIC, payload)

    # ---- input from the proxy

    def on_request(self, res_act, data):
        self.last_data = dt.datetime.now(dt.timezone.utc)
        ident = data.get("1", "")
        if "#" in ident and not self.mac:
            self.mac = ident.split("#", 1)[0]
        try:
            if res_act == "device/edit" and "2" in data:
                self._device(data)
            elif res_act == "zone/edit_all":
                changed = False
                for z in data.get("data", []):
                    num, name = str(z.get("2")), (z.get("3") or "").strip()
                    if name and self.zones.get(num) != name:     # an empty name never erases one
                        self.zones[num] = name
                        changed = True
                if changed:
                    self._save_json(self.zones_file, self.zones)
                for dev in list(self.devices):
                    self._announce_device(dev)
            elif res_act == "smarthome/edit":
                new = {k: v for k, v in data.items() if k != "1"}
                # "12" is the clock of the central unit: it always changes, it is not an event.
                changed = {k: f"{self.central[k]} -> {v}" for k, v in new.items()
                           if k in self.central and self.central[k] != v and k != "12"}
                fw_before = (self.central.get("6"), self.central.get("7"))
                self.central.update(new)
                if changed:
                    self.event(f"central unit: {changed}")
                if (self.central.get("6"), self.central.get("7")) != fw_before:
                    self.central_announced = False   # announce again with firmware/hardware
                self._publish_central()
            elif res_act in ("device/edit_all", "smarthome/edit_mode"):
                key = res_act + ":" + str(data.get("28", data.get("27", "")))
                val = json.dumps({k: v for k, v in data.items() if k != "1"}, ensure_ascii=False)
                if self.central.get(key) not in (None, val):
                    self.event(f"{res_act} changed: {val}")
                self.central[key] = val
        except Exception as e:  # noqa: BLE001
            self.log(f"[ha] error processing {res_act}: {type(e).__name__}: {e}")

    def on_response(self, j):
        d = j.get("data") if isinstance(j, dict) else None
        if not isinstance(d, dict) or j.get("code") != "1" or "3" not in d:
            return
        cmd = d["3"]
        dev = cmd.get("2", "?")
        parts = ", ".join(f"{FIELD_NAMES.get(k, 'field ' + k)}={show(k, v)}"
                          for k, v in cmd.items() if k != "2")
        self.event(f"CLOUD COMMAND for {dev}: {parts}",
                   {"type": "cloud_command", "thermostat": dev, "fields": cmd})

    # ---- thermostats

    def _device(self, data):
        dev = data["2"]
        fields = {k: v for k, v in data.items() if k != "1"}
        prev = self.devices.get(dev)
        valid6 = isinstance(fields.get("6"), str) and len(fields["6"]) >= 336
        if "6" in fields and not valid6:
            # Empty or incomplete schedule: it never replaces a good one.
            bad = fields.pop("6")
            if (prev or {}).get("_6_bad") != bad:
                self.log(f"[ha] {dev}: invalid schedule ignored (length {len(str(bad))})")
            fields["_6_bad"] = bad
        if prev:
            changes = []
            for k, v in fields.items():
                if k.startswith("_"):
                    continue
                if prev.get(k) != v:
                    if k == "6":
                        changes.append("schedule changed")
                    else:
                        changes.append(f"{FIELD_NAMES.get(k, 'field ' + k)}: "
                                       f"{show(k, prev.get(k))} -> {show(k, v)}")
            # The measured temperature changes often: only logged together with something else.
            relevant = [c for c in changes if not c.startswith("temperature:")]
            if relevant:
                self.event(f"{dev}: " + "; ".join(changes),
                           {"type": "change", "thermostat": dev,
                            "before": {k: prev.get(k) for k in fields if prev.get(k) != fields[k] and k != "6"},
                            "after": {k: v for k, v in fields.items() if prev.get(k) != v and k != "6"}})
        # The central unit does not always send every field: merge with what we had.
        merged = {**(prev or {}), **fields}
        if not (isinstance(merged.get("6"), str) and len(merged["6"]) >= 336) and dev in self.schedules:
            merged["6"] = self.schedules[dev]
        self.devices[dev] = merged
        if valid6 and self.schedules.get(dev) != fields["6"]:
            self.schedules[dev] = fields["6"]
            self._save_json(self.schedules_file, self.schedules)
        self.dev_seen[dev] = dt.datetime.now(dt.timezone.utc)
        self._check_confirmation(dev, fields)
        self._check_fields(dev, fields)
        self._set_zone_reason(dev, "no_reports", False, "")
        self._announce_device(dev)
        self._publish_device(dev)

    def _slug(self, dev):
        return dev.lower().replace("-", "_")

    def _uid(self, *parts):
        base = (self.mac or "central").replace(":", "").lower()
        return "_".join(["watts", base] + [p for p in parts if p])

    def _central_device(self):
        d = {"identifiers": [self._uid()], "name": self.t("central"),
             "manufacturer": "Watts", "model": "BT-CT02-RF",
             "connections": [["mac", self.mac]] if self.mac else []}
        # HA rejects the whole discovery message if sw_version/hw_version is null.
        if self.central.get("6"):
            d["sw_version"] = str(self.central["6"])
        if self.central.get("7"):
            d["hw_version"] = str(self.central["7"])
        return d

    def _zone_number(self, dev):
        z = str(self.devices.get(dev, {}).get("4", ""))
        return z or self._slug(dev)

    def _zone_name(self, dev):
        z = str(self.devices.get(dev, {}).get("4", ""))
        return self.zones.get(z) or (self.t("zone", n=z) if z else dev)

    def _announce_device(self, dev):
        if not self.mqtt or not self.mac:
            return
        name = self._zone_name(dev)
        if self.announced.get(dev) == name:
            return
        self.announced[dev] = name
        slug = self._slug(dev)
        self.slugs[slug] = dev
        # Default entity_id: climate.watts_zone_1, sensor.watts_zone_1_temperature...
        # Only used when HA creates the entity; existing entities keep theirs.
        base_id = f"watts_zone_{self._zone_number(dev)}"
        state = f"watts_proxy/{slug}/state"
        device = {"identifiers": [self._uid(slug)], "name": f"Watts {name}",
                  "manufacturer": "Watts", "model": self.t("thermostat_model", dev=dev),
                  "via_device": self._uid()}
        common = {"state_topic": state, "availability_topic": STATUS_TOPIC,
                  "expire_after": 900, "device": device}

        def temp(key, **extra):
            return ("sensor", key, {"device_class": "temperature",
                                    "unit_of_measurement": "°C", "state_class": "measurement",
                                    "suggested_display_precision": 1,
                                    "value_template": "{{ value_json.%s }}" % key, **extra})

        # (component, key, legacy unique_id suffix, config). The unique_id suffixes come from
        # the first versions and must not change: HA would create new entities.
        entities = [
            (*temp("temperature"), "temperatura"),
            (*temp("setpoint", entity_category="diagnostic"), "consigna"),
            (*temp("comfort_setpoint", entity_category="diagnostic"), "consigna_manual"),
            (*temp("eco_setpoint", entity_category="diagnostic"), "consigna_eco"),
            (*temp("field_11", entity_category="diagnostic", enabled_by_default=False), "consigna_programa"),
            (*temp("boost_setpoint", entity_category="diagnostic"), "campo_10"),
            (*temp("frost_setpoint", entity_category="diagnostic"), "antihielo"),
            ("sensor", "boost_remaining", {"icon": "mdi:timer-sand", "unit_of_measurement": "min",
                                           "entity_category": "diagnostic",
                                           "value_template": "{{ value_json.boost_remaining }}"},
             "boost_restante"),
            ("sensor", "period", {"icon": "mdi:clock-outline", "entity_category": "diagnostic",
                                  "value_template": "{{ value_json.period or 'none' }}"}, "tramo"),
            ("sensor", "next_change", {"device_class": "timestamp",
                                       "value_template": "{{ value_json.next_change or 'None' }}",
                                       "json_attributes_topic": state,
                                       "json_attributes_template":
                                           "{{ {'schedule': value_json.schedule, 'values': value_json.schedule_values,"
                                           " 'week': value_json.schedule_week} | tojson }}"}, "proximo_cambio"),
            ("sensor", "mode", {"icon": "mdi:calendar-clock", "entity_category": "diagnostic",
                                "value_template": "{{ value_json.mode }}"}, "modo"),
            ("binary_sensor", "heat_demand", {"device_class": "heat",
                                              "value_template": "{{ value_json.heat_demand }}",
                                              "payload_on": "ON", "payload_off": "OFF"}, "demanda"),
            # Health: no expire_after, they must keep warning even if the zone goes silent.
            ("binary_sensor", "warning", {"device_class": "problem", "expire_after": 0,
                                          "value_template": "{{ value_json.warning }}",
                                          "payload_on": "ON", "payload_off": "OFF",
                                          "json_attributes_topic": state,
                                          "json_attributes_template":
                                              "{{ {'reasons': value_json.warning_reasons,"
                                              " 'unidentified_fields': value_json.watched_fields} | tojson }}"},
             "aviso"),
            ("sensor", "last_report", {"device_class": "timestamp", "entity_category": "diagnostic",
                                       "expire_after": 0, "value_template": "{{ value_json.last_report }}"},
             "ultimo_informe"),
            ("sensor", "field_14", {"entity_category": "diagnostic", "enabled_by_default": False,
                                    "value_template": "{{ value_json.raw['14'] }}"}, "campo_14"),
        ]
        for component, key, cfg, uid_key in entities:
            uid = self._uid(slug, uid_key)
            conf = {**common, **cfg, "name": self.name(key), "unique_id": uid,
                    "default_entity_id": f"{component}.{base_id}_{key}"}
            if key in ("temperature", "setpoint", "mode", "heat_demand"):
                conf["json_attributes_topic"] = state
                conf["json_attributes_template"] = "{{ value_json.raw | tojson }}"
            self.mqtt.publish(f"{self.prefix}/{component}/{uid}/config", conf, retain=True)

        # Control entity: only with allow_control; otherwise any previous one is removed.
        cuid = self._uid(slug, "termostato")
        ctopic = f"{self.prefix}/climate/{cuid}/config"
        if self.allow_control:
            self.mqtt.publish(ctopic, {
                "name": None, "unique_id": cuid, "default_entity_id": f"climate.{base_id}",
                "device": device, "availability_topic": STATUS_TOPIC,
                "modes": ["off", "heat", "auto"],
                "mode_state_topic": state,
                "mode_state_template": "{{ value_json.hvac }}",
                "mode_command_topic": f"watts_proxy/{slug}/set/mode",
                "preset_modes": PRESETS,
                "preset_mode_state_topic": state,
                "preset_mode_value_template": "{{ value_json.preset }}",
                "preset_mode_command_topic": f"watts_proxy/{slug}/set/preset",
                "temperature_state_topic": state,
                "temperature_state_template": "{{ value_json.setpoint }}",
                "temperature_command_topic": f"watts_proxy/{slug}/set/temperature",
                "current_temperature_topic": state,
                "current_temperature_template": "{{ value_json.temperature }}",
                "action_topic": state,
                "action_template": "{{ value_json.action }}",
                "min_temp": 5, "max_temp": 30, "temp_step": 0.5, "precision": 0.1,
                "temperature_unit": "C",
            }, retain=True)
        else:
            self.mqtt.publish(ctopic, "", retain=True)

        # Editable schedule (text): only with allow_control.
        huid = self._uid(slug, "horario_editable")
        htopic = f"{self.prefix}/text/{huid}/config"
        if self.allow_control:
            self.mqtt.publish(htopic, {
                "name": self.name("schedule"), "unique_id": huid,
                "default_entity_id": f"text.{base_id}_schedule",
                "device": device, "availability_topic": STATUS_TOPIC, "icon": "mdi:calendar-edit",
                "state_topic": state, "value_template": "{{ value_json.schedule_text }}",
                "command_topic": f"watts_proxy/{slug}/set/schedule",
                # For the graphical card: the raw week (336 characters do not fit in the state of
                # a text entity, max. 255), the topic to publish to with mqtt.publish, and whether
                # a command is waiting for confirmation.
                "json_attributes_topic": state,
                "json_attributes_template":
                    "{{ {'week': value_json.schedule_week, 'pending': value_json.pending,"
                    " 'command_topic': 'watts_proxy/%s/set/schedule', 'zone': '%s'} | tojson }}"
                    % (slug, name.replace("'", "")),
                "max": 255, "mode": "text",
            }, retain=True)
        else:
            self.mqtt.publish(htopic, "", retain=True)
        self.log(f"[ha] announced {dev} as 'Watts {name}'")
        self._announce_central()

    def _publish_device(self, dev):
        if not self.mqtt:
            return
        overlay = self._overlay(dev)
        f = {**self.devices[dev], **overlay}
        mode = str(f.get("22", ""))
        setpoint, period = active_setpoint(f)
        hvac, preset, action = climate_view(f)
        try:
            boost_left = int(f.get("14") or 0) // 60 if mode == "4" else 0
        except ValueError:
            boost_left = 0
        week = f.get("6") if isinstance(f.get("6"), str) and len(f["6"]) >= 336 else None
        text = schedule_text(week, self.lang)
        state = {
            "hvac": hvac,
            "preset": preset,
            "action": action,
            "temperature": f10_to_c(f.get("16")),
            "setpoint": setpoint,
            "period": period,
            "mode": MODES.get(mode, f"unknown ({mode})"),
            "heat_demand": "ON" if str(f.get("33")) == "1" else "OFF",
            "boost_remaining": boost_left,
            "comfort_setpoint": f10_to_c(f.get("7")),
            "eco_setpoint": f10_to_c(f.get("9")),
            "frost_setpoint": f10_to_c(f.get("8")),
            "boost_setpoint": f10_to_c(f.get("10")),
            "field_11": f10_to_c(f.get("11")),
            "pending": bool(overlay),
            "warning": "ON" if self.zone_alerts.get(dev) else "OFF",
            "warning_reasons": list((self.zone_alerts.get(dev) or {}).values()),
            "watched_fields": {k: f.get(k) for k in WATCH_FIELDS},
            "last_report": (lambda t: t.isoformat(timespec="seconds") if t else None)(self.dev_seen.get(dev)),
            "next_change": (lambda n: n.isoformat(timespec="minutes") if n else None)(
                next_change(week) if mode in PROGRAM_MODES else None),
            "schedule": schedule_summary(week, self.lang),
            "schedule_values": "".join(sorted(set(week or ""))),
            # Editable text: the state of an entity is limited to 255 characters.
            "schedule_text": text if len(text) <= 255 else text[:252] + "...",
            "schedule_week": week,
            "raw": {**{k: v for k, v in f.items() if k != "6" and not k.startswith("_")},
                    "6_len": len(f.get("6") or "")},
        }
        self.mqtt.publish(f"watts_proxy/{self._slug(dev)}/state", state, retain=True)
        self._publish_central()

    # ---- central unit

    def _announce_central(self):
        if self.central_announced or not self.mqtt or not self.mac:
            return
        self.central_announced = True
        state = "watts_proxy/central/state"
        common = {"state_topic": state, "availability_topic": STATUS_TOPIC,
                  "device": self._central_device()}
        ents = [
            ("binary_sensor", "any_demand", {"device_class": "heat", "expire_after": 900,
                                             "value_template": "{{ value_json.heat_demand }}",
                                             "payload_on": "ON", "payload_off": "OFF"}, "demanda_total"),
            ("sensor", "zones_with_demand", {"icon": "mdi:radiator", "state_class": "measurement",
                                             "expire_after": 900,
                                             "value_template": "{{ value_json.zones_with_demand }}"},
             "zonas_con_demanda"),
            ("sensor", "connection", {"icon": "mdi:cloud-sync", "device_class": "enum",
                                      "options": ["cloud", "local"],
                                      "value_template": "{{ value_json.connection }}"}, "conexion"),
            ("switch", "force_local", {"icon": "mdi:cloud-off-outline",
                                       "command_topic": "watts_proxy/central/set/force_local",
                                       "value_template": "{{ value_json.force_local }}",
                                       "payload_on": "ON", "payload_off": "OFF",
                                       "state_on": "ON", "state_off": "OFF"}, "forzar_local"),
            ("number", "boost_duration", {"icon": "mdi:timer-outline",
                                          "command_topic": "watts_proxy/central/set/boost_minutes",
                                          "value_template": "{{ value_json.boost_minutes }}",
                                          "min": 5, "max": 240, "step": 5,
                                          "unit_of_measurement": "min", "entity_category": "config"},
             "boost_minutos"),
            ("sensor", "last_data", {"device_class": "timestamp", "entity_category": "diagnostic",
                                     "value_template": "{{ value_json.last_data }}"}, "ultimo_dato"),
            ("binary_sensor", "communication", {"device_class": "connectivity",
                                                "value_template": "{{ value_json.communication }}",
                                                "payload_on": "ON", "payload_off": "OFF"}, "comunicacion"),
            ("sensor", "status", {"icon": "mdi:heart-pulse", "device_class": "enum",
                                  "options": ["ok", "warning", "problem"],
                                  "value_template": "{{ value_json.status }}",
                                  "json_attributes_topic": state,
                                  "json_attributes_template": "{{ {'problems': value_json.problems} | tojson }}"},
             "estado"),
            ("event", "events", {"icon": "mdi:bell-ring-outline",
                                 "state_topic": EVENT_ENTITY_TOPIC, "event_types": EVENT_TYPES}, "eventos"),
        ]
        for component, key, cfg, uid_key in ents:
            uid = self._uid("central", uid_key)
            conf = {**common, **cfg, "name": self.name(key), "unique_id": uid,
                    "default_entity_id": f"{component}.watts_central_{key}"}
            self.mqtt.publish(f"{self.prefix}/{component}/{uid}/config", conf, retain=True)

    def _publish_central(self):
        if not self.mqtt or not self.mac:
            return
        self._announce_central()
        demand = [d for d, f in self.devices.items() if str(f.get("33")) == "1"]
        status, problems = self.health()
        self.mqtt.publish("watts_proxy/central/state", {
            "heat_demand": "ON" if demand else "OFF",
            "zones_with_demand": len(demand),
            "zones": demand,
            "last_data": self.last_data.isoformat(timespec="seconds") if self.last_data else None,
            "firmware": self.central.get("6"), "hardware": self.central.get("7"),
            "connection": self.connection, "reason": self.connection_reason,
            "force_local": "ON" if self.force_local else "OFF",
            "boost_minutes": self.settings.get("boost_minutes", BOOST_DEFAULT_MIN),
            "communication": "OFF" if self.central_ok is False else "ON",
            "status": status,
            "problems": problems,
        }, retain=True)
