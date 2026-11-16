"""Shared helpers for the tests. No real data: the MAC and schedules are made up."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "watts_proxy"))

# proxy.py reads these at import time: point them to an empty temporary directory.
_TMP = tempfile.mkdtemp(prefix="watts_proxy_tests_")
os.environ.setdefault("WP_OPTIONS", os.path.join(_TMP, "options.json"))
os.environ.setdefault("WP_LOG_DIR", _TMP)
os.environ.setdefault("WP_DATA", _TMP)

import watts_ha  # noqa: E402

MAC = "00:11:22:33:44:55"
DEV = "C002-001"


def week(*days):
    """336-char schedule from 7 lists of (start, end) comfort periods in half-hour slots."""
    out = ""
    for periods in days:
        day = ["0"] * 48
        for a, b in periods:
            for i in range(a, b):
                day[i] = "5"
        out += "".join(day)
    return out


# Mon-Fri 06:00-15:30, Sat-Sun all eco.
OFFICE = week(*[[(12, 31)]] * 5, [], [])


class FakeMqtt:
    """Records what would be published; nothing goes to the network."""

    def __init__(self):
        self.published = []      # (topic, payload, retain)
        self.subs = {}
        self.connected = type("E", (), {"is_set": lambda self: True})()

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, cb):
        self.subs[topic] = cb

    def start(self):
        pass

    def last(self, topic):
        for t, p, _ in reversed(self.published):
            if t == topic:
                return p
        return None

    def events(self):
        return [p for t, p, _ in self.published if t == watts_ha.EVENT_ENTITY_TOPIC]

    def configs(self):
        return {t: p for t, p, _ in self.published if t.endswith("/config") and p}


def device_fields(mode="8", **extra):
    f = {"2": DEV, "4": "2", "6": OFFICE, "7": "716", "8": "446", "9": "680", "10": "752",
         "11": "716", "14": "7200", "16": "698", "22": mode, "23": "0", "33": "0", "34": "0", "35": "0"}
    f.update(extra)
    return f


def make_state(lang="en", allow_control=True, data_dir=None, **opt):
    data_dir = data_dir or tempfile.mkdtemp(prefix="watts_state_")
    s = watts_ha.WattsState({"allow_control": allow_control, "language": lang, "mqtt_enabled": True, **opt},
                            lambda m: None, data_dir, data_dir)
    s.mqtt = FakeMqtt()
    s.on_request("smarthome/edit", {"1": f"{MAC}#1793829069", "6": "03.63", "7": "02.48"})
    return s


def report(state, **fields):
    """The central unit reports a thermostat (device/edit)."""
    state.on_request("device/edit", {"1": f"{MAC}#1793829069", **device_fields(**fields)})
