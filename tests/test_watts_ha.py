import asyncio
import datetime as dt
import json
import os
import tempfile
import unittest

from common import DEV, MAC, OFFICE, make_state, report, week, watts_ha

SLUG = "c002_001"


def cmd(state, what, payload):
    state._on_command(f"watts_proxy/{SLUG}/set/{what}", payload)


def queued(state, kind="mode"):
    item = state.queue.get(f"{DEV}|{kind}")
    return item["cmd"] if item else None


class Conversions(unittest.TestCase):
    def test_f10(self):
        self.assertEqual(watts_ha.f10_to_c("716"), 22.0)
        self.assertEqual(watts_ha.f10_to_c("446"), 7.0)
        self.assertIsNone(watts_ha.f10_to_c(None))
        self.assertIsNone(watts_ha.f10_to_c("x"))

    def test_c_to_f10(self):
        self.assertEqual(watts_ha.c_to_f10(22), "716")
        self.assertEqual(watts_ha.c_to_f10("20"), "680")
        self.assertEqual(watts_ha.c_to_f10(21.3), "707")      # rounded to 21.5
        self.assertEqual(watts_ha.c_to_f10(40), "860")        # limited to 30
        self.assertEqual(watts_ha.c_to_f10(1), "410")         # limited to 5

    def test_round_trip(self):
        for half in range(10, 61):
            c = half / 2
            self.assertEqual(watts_ha.f10_to_c(watts_ha.c_to_f10(c)), c)


class Schedules(unittest.TestCase):
    def test_summary_en_es(self):
        self.assertEqual(watts_ha.schedule_text(OFFICE), "Mon–Fri: 06:00–15:30; Sat–Sun: all eco")
        self.assertEqual(watts_ha.schedule_text(OFFICE, "es"), "Lun–Vie: 06:00–15:30; Sáb–Dom: todo eco")

    def test_grouping_not_consecutive(self):
        s = week([(11, 19)], [], [(11, 19)], [], [], [], [(0, 48)])
        self.assertEqual(watts_ha.schedule_text(s), "Mon, Wed: 05:30–09:30; Tue, Thu–Sat: all eco; Sun: 00:00–24:00")

    def test_other_levels(self):
        s = "1" * 2 + "0" * 334
        self.assertIn("[level 1]", watts_ha.schedule_text(s))
        self.assertIn("[nivel 1]", watts_ha.schedule_text(s, "es"))

    def test_parse_round_trip(self):
        samples = [OFFICE, week([(11, 19), (27, 32), (35, 41)], [(11, 19), (27, 30), (36, 41)],
                                [(11, 19)], [], [(11, 19), (27, 34)], [(11, 41)], [(37, 46)]),
                   "0" * 336, "5" * 336]
        for s in samples:
            for lang in ("en", "es"):
                self.assertEqual(watts_ha.parse_schedule(watts_ha.schedule_text(s, lang)), s)

    def test_parse_formats(self):
        a = watts_ha.parse_schedule("Mon-Fri: 6-15:30\nSat–Sun: all eco")
        self.assertEqual(a, OFFICE)
        b = watts_ha.parse_schedule("lun - vie: 06:00 – 15:30; sabado-domingo: eco")
        self.assertEqual(b, OFFICE)
        self.assertEqual(watts_ha.parse_schedule("all: all comfort"), "5" * 336)
        self.assertEqual(watts_ha.parse_schedule("todos: todo confort"), "5" * 336)
        self.assertEqual(watts_ha.parse_schedule("5" * 336), "5" * 336)

    def test_parse_keeps_other_days(self):
        new = watts_ha.parse_schedule("Sat: 09:00–13:00", OFFICE)
        self.assertEqual(new[:5 * 48], OFFICE[:5 * 48])
        self.assertEqual(new[5 * 48 + 18:5 * 48 + 26], "5" * 8)
        self.assertEqual(new[6 * 48:], "0" * 48)

    def test_parse_errors(self):
        for bad in ["", "Xyz: 06:00-07:00", "Fri-Mon: eco", "Mon: 06:15-07:00", "Mon: 08:00-07:00",
                    "Mon: eco; Mon: comfort", "06:00-07:00", "Mon: 25:00-26:00", "Mon: 06:00"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                watts_ha.parse_schedule(bad)

    def test_period_and_next_change(self):
        monday_7 = dt.datetime(2026, 11, 16, 7, 10)
        self.assertEqual(watts_ha.program_period(OFFICE, monday_7), "comfort")
        self.assertEqual(watts_ha.program_period(OFFICE, monday_7.replace(hour=20)), "eco")
        nxt = watts_ha.next_change(OFFICE, monday_7)
        self.assertEqual((nxt.hour, nxt.minute, nxt.day), (15, 30, 16))
        friday_20 = dt.datetime(2026, 11, 20, 20, 0)
        nxt = watts_ha.next_change(OFFICE, friday_20)
        self.assertEqual((nxt.day, nxt.hour), (23, 6))          # Monday 06:00
        self.assertIsNone(watts_ha.next_change("0" * 336, monday_7))


class ClimateView(unittest.TestCase):
    def test_modes(self):
        monday_7 = dt.datetime(2026, 11, 16, 7, 10)
        f = {"6": OFFICE, "7": "716", "8": "446", "9": "680", "10": "752", "11": "500", "33": "1"}
        self.assertEqual(watts_ha.climate_view({**f, "22": "8"}), ("auto", "none", "heating"))
        self.assertEqual(watts_ha.climate_view({**f, "22": "3", "33": "0"}), ("heat", "eco", "idle"))
        self.assertEqual(watts_ha.climate_view({**f, "22": "2"}), ("heat", "away", "heating"))
        self.assertEqual(watts_ha.climate_view({**f, "22": "1"}), ("off", "none", "off"))
        self.assertEqual(watts_ha.active_setpoint({**f, "22": "8"}, monday_7), (22.0, "comfort"))
        self.assertEqual(watts_ha.active_setpoint({**f, "22": "11"}, monday_7.replace(hour=22)), (20.0, "eco"))
        self.assertEqual(watts_ha.active_setpoint({**f, "22": "4"}), (24.0, None))
        self.assertEqual(watts_ha.active_setpoint({**f, "22": "2"}), (7.0, None))
        self.assertEqual(watts_ha.active_setpoint({**f, "22": "1"}), (None, None))


class Commands(unittest.TestCase):
    def setUp(self):
        self.s = make_state()
        report(self.s)

    def test_eco_frost_off_program_heat(self):
        cmd(self.s, "preset", "eco")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "9": "680", "11": "680", "22": "3", "15": "3"})
        cmd(self.s, "preset", "away")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "8": "446", "11": "446", "22": "2", "15": "2"})
        cmd(self.s, "preset", "antihielo")                     # name before 0.7.2
        self.assertEqual(queued(self.s)["22"], "2")
        cmd(self.s, "mode", "off")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "11": "0", "22": "1", "15": "1"})
        cmd(self.s, "mode", "auto")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "22": "11", "15": "11", "11": "680"})
        cmd(self.s, "mode", "heat")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "7": "716", "11": "716", "22": "0", "15": "0"})

    def test_boost_uses_configured_duration(self):
        self.s._on_central("watts_proxy/central/set/boost_minutes", "30")
        cmd(self.s, "preset", "boost")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "1800", "10": "752", "11": "752", "22": "4", "15": "4"})
        self.s._on_central("watts_proxy/central/set/boost_minutos", "500")     # name before 0.9.0
        self.assertEqual(self.s.settings["boost_minutes"], 240)

    def test_temperature_follows_current_mode(self):
        cmd(self.s, "temperature", "21")                        # program -> manual
        self.assertEqual(queued(self.s)["22"], "0")
        self.assertEqual(queued(self.s)["7"], "698")
        self.s.queue.clear()
        report(self.s, mode="3")
        cmd(self.s, "temperature", "19")
        self.assertEqual(queued(self.s), {"2": DEV, "14": "0", "9": "662", "11": "662", "22": "3", "15": "3"})
        self.s.queue.clear()
        report(self.s, mode="4", **{"14": "900"})
        cmd(self.s, "temperature", "25")
        self.assertEqual(queued(self.s)["14"], "900")           # remaining boost time is kept
        self.s.queue.clear()
        report(self.s, mode="2")
        cmd(self.s, "temperature", "15")
        self.assertEqual(queued(self.s)["8"], "500")            # frost protection: max 10 °C

    def test_boost_from_off_goes_through_manual(self):
        report(self.s, mode="1")
        cmd(self.s, "preset", "boost")
        item = self.s.queue[f"{DEV}|mode"]
        self.assertEqual(item["cmd"]["22"], "0")
        self.assertEqual(item["then"][0]["22"], "4")
        # HA already shows the final result (boost).
        state = self.s.mqtt.last(f"watts_proxy/{SLUG}/state")
        self.assertEqual(state["preset"], "boost")
        self.assertTrue(state["pending"])

        async def run():
            self.s.transform_response(_no_command(), "query/check")
            report(self.s, mode="0")                             # the central unit confirms manual
            return queued(self.s)
        nxt = asyncio.run(run())
        self.assertEqual(nxt["22"], "4")
        self.assertIn("command_confirmed", [e["event_type"] for e in self.s.mqtt.events()])

    def test_schedule(self):
        cmd(self.s, "schedule", "Sun: 05:30–14:30")
        sent = queued(self.s, "schedule")
        self.assertEqual(set(sent), {"2", "6"})
        self.assertEqual(sent["6"][:6 * 48], OFFICE[:6 * 48])
        self.assertEqual(sent["6"][6 * 48 + 11:6 * 48 + 29], "5" * 18)
        cmd(self.s, "horario", "Dom: 05:30–14:30")              # topic before 0.9.0
        self.assertEqual(queued(self.s, "schedule"), sent)
        self.assertIsNone(queued(self.s))                       # independent of the mode slot

    def test_schedule_rejected(self):
        cmd(self.s, "schedule", "Funday: 06:00-07:00")
        self.assertIsNone(queued(self.s, "schedule"))
        ev = self.s.mqtt.events()[-1]
        self.assertEqual(ev["event_type"], "schedule_rejected")
        self.assertEqual(ev["thermostat"], DEV)

    def test_unknown_thermostat_and_command(self):
        self.s._on_command("watts_proxy/c009_008/set/mode", "off")
        cmd(self.s, "mode", "cool")
        self.assertEqual(self.s.queue, {})


def _no_command(code="2"):
    body = json.dumps({"code": code, "data": {"100": "x"}}).encode()
    return b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


class Delivery(unittest.TestCase):
    def setUp(self):
        self.s = make_state()
        report(self.s)

    def test_injection_and_retries(self):
        cmd(self.s, "preset", "eco")

        async def run():
            self.assertIs(self.s.transform_response(_no_command("1"), "query/check").__class__, bytes)
            same = _no_command()
            self.assertIs(self.s.transform_response(same, "device/edit"), same)     # not query/check
            out = self.s.transform_response(same, "query/check")
            head, _, body = out.partition(b"\r\n\r\n")
            j = json.loads(body)
            self.assertEqual(j["code"], "1")
            self.assertEqual(j["data"]["3"]["22"], "3")
            self.assertIn(f"Content-Length: {len(body)}".encode(), head)
            key = f"{DEV}|mode"
            for attempt in (1, 2, 3):
                self.assertEqual(self.s.pending[key]["attempts"], attempt)
                self.s.pending[key]["sent"] -= 50                # 45 s without confirmation
                if attempt < 3:
                    self.s.transform_response(_no_command(), "query/check")
            self.s._due_retries()
            self.assertNotIn(key, self.s.pending)
            self.assertNotIn(key, self.s.queue)
        asyncio.run(run())
        types = [e["event_type"] for e in self.s.mqtt.events()]
        self.assertIn("command_failed", types)
        status, problems = self.s.health()
        self.assertEqual(status, "warning")
        self.assertTrue(any("eco" in p for p in problems))

    def test_cloud_command_is_never_replaced(self):
        cmd(self.s, "preset", "eco")
        msg = _no_command("1")

        async def run():
            return self.s.transform_response(msg, "query/check")
        self.assertIs(asyncio.run(run()), msg)
        self.assertIn(f"{DEV}|mode", self.s.queue)

    def test_read_only(self):
        s = make_state(allow_control=False)
        report(s)
        msg = _no_command()
        self.assertIs(s.transform_response(msg, "query/check"), msg)
        climate = [t for t, p, _ in s.mqtt.published if "/climate/" in t]
        self.assertTrue(climate and all(p == "" for t, p, _ in s.mqtt.published if "/climate/" in t))


class Discovery(unittest.TestCase):
    def test_entities(self):
        s = make_state()
        s.on_request("zone/edit_all", {"1": "x", "data": [{"2": "2", "3": "Living room"}]})
        report(s)
        conf = s.mqtt.configs()
        uid = f"watts_{MAC.replace(':', '').lower()}_{SLUG}"
        climate = conf[f"homeassistant/climate/{uid}_termostato/config"]
        self.assertEqual(climate["default_entity_id"], "climate.watts_zone_2")
        self.assertEqual(climate["device"]["name"], "Watts Living room")
        temp = conf[f"homeassistant/sensor/{uid}_temperatura/config"]      # legacy unique_id kept
        self.assertEqual(temp["default_entity_id"], "sensor.watts_zone_2_temperature")
        self.assertEqual(temp["name"], "Temperature")
        text = conf[f"homeassistant/text/{uid}_horario_editable/config"]
        self.assertEqual(text["command_topic"], f"watts_proxy/{SLUG}/set/schedule")
        central = conf[f"homeassistant/event/watts_{MAC.replace(':', '').lower()}_central_eventos/config"]
        self.assertEqual(central["event_types"], watts_ha.EVENT_TYPES)
        self.assertEqual(central["device"]["sw_version"], "03.63")
        for c in conf.values():
            self.assertNotIn("object_id", c)

    def test_spanish_names(self):
        s = make_state(lang="es")
        report(s)
        names = {c["name"] for c in s.mqtt.configs().values() if c.get("name")}
        self.assertTrue({"Temperatura", "Horario", "Modo local (sin nube)", "Eventos"} <= names)
        state = s.mqtt.last(f"watts_proxy/{SLUG}/state")
        self.assertEqual(state["schedule_text"], "Lun–Vie: 06:00–15:30; Sáb–Dom: todo eco")
        self.assertEqual(state["mode"], "program")              # machine values stay in English

    def test_state(self):
        s = make_state()
        report(s, **{"33": "1"})
        st = s.mqtt.last(f"watts_proxy/{SLUG}/state")
        self.assertEqual((st["hvac"], st["action"], st["temperature"]), ("auto", "heating", 21.0))
        self.assertEqual(st["schedule_week"], OFFICE)
        self.assertEqual(st["raw"]["6_len"], 336)
        central = s.mqtt.last("watts_proxy/central/state")
        self.assertEqual((central["heat_demand"], central["zones_with_demand"]), ("ON", 1))
        self.assertEqual(central["connection"], "cloud")

    def test_bad_schedule_does_not_replace_good_one(self):
        s = make_state()
        report(s)
        report(s, **{"6": "0" * 10})
        self.assertEqual(s.devices[DEV]["6"], OFFICE)


class Health(unittest.TestCase):
    def test_central_silent_and_back(self):
        s = make_state()
        report(s)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
        s.check_health(later)
        self.assertEqual(s.mqtt.last("watts_proxy/central/state")["status"], "problem")
        self.assertEqual(s.mqtt.events()[-1]["event_type"], "central_no_data")
        report(s)
        s.check_health()
        self.assertEqual(s.mqtt.events()[-1]["event_type"], "central_restored")
        self.assertEqual(s.mqtt.last("watts_proxy/central/state")["status"], "ok")

    def test_silent_zone(self):
        s = make_state()
        report(s)
        s.dev_seen[DEV] -= dt.timedelta(minutes=40)
        s.check_health()
        ev = s.mqtt.events()[-1]
        self.assertEqual((ev["event_type"], ev["thermostat"]), ("thermostat_warning", DEV))
        self.assertEqual(s.mqtt.last(f"watts_proxy/{SLUG}/state")["warning"], "ON")
        report(s)
        self.assertEqual(s.mqtt.last(f"watts_proxy/{SLUG}/state")["warning"], "OFF")

    def test_unidentified_fields(self):
        s = make_state(lang="es")
        report(s, **{"34": "1"})
        st = s.mqtt.last(f"watts_proxy/{SLUG}/state")
        self.assertEqual(st["warning"], "ON")
        self.assertIn("campo 34 = 1", st["warning_reasons"][0])
        report(s, **{"99": "x"})                                # unknown new field
        self.assertTrue(any("99" in r for r in s.mqtt.last(f"watts_proxy/{SLUG}/state")["warning_reasons"]))

    def test_cloud_events_not_for_forced_local(self):
        s = make_state()
        s.set_connection(True, "forced local mode (HA switch)")
        s.set_connection(False)
        s.set_connection(True, "TimeoutError")
        s.set_connection(False)
        self.assertEqual([e["event_type"] for e in s.mqtt.events()], ["cloud_unavailable", "cloud_restored"])


class Migration(unittest.TestCase):
    def test_old_data_files(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "ajustes.json"), "w") as f:
            json.dump({"boost_minutos": 45}, f)
        with open(os.path.join(d, "horarios.json"), "w") as f:
            json.dump({DEV: OFFICE}, f)
        with open(os.path.join(d, "zonas.json"), "w") as f:
            json.dump({"2": "Kitchen"}, f)
        s = make_state(data_dir=d)
        self.assertEqual(s.settings["boost_minutes"], 45)
        self.assertEqual(s.schedules[DEV], OFFICE)
        self.assertEqual(s.zones["2"], "Kitchen")
        self.assertTrue(os.path.exists(os.path.join(d, "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(d, "horarios.json")))


if __name__ == "__main__":
    unittest.main()
