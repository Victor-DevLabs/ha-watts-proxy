import asyncio
import json
import time
import unittest
import urllib.parse

from common import DEV, MAC, FakeMqtt, device_fields, make_state

import proxy


def request(res_act, data):
    """HTTP request as the central unit sends it (field 1 obfuscated)."""
    data = dict(data)
    data["1"] = proxy.watts_encode(f"{MAC}#{int(time.time())}")
    path = urllib.parse.quote(json.dumps(data, separators=(",", ":")))
    return (f"GET http://smarthome.wattselectronics.com/api/v0.1/machine/{res_act}/{path} HTTP/1.1\r\n"
            "Host: smarthome.wattselectronics.com\r\n\r\n").encode()


def body_of(msg):
    return json.loads(msg.partition(b"\r\n\r\n")[2])


class Protocol(unittest.TestCase):
    def test_obfuscation(self):
        s = f"{MAC}#1793829069"
        self.assertNotEqual(proxy.watts_encode(s), s)
        self.assertEqual(proxy.watts_decode(proxy.watts_encode(s)), s)

    def test_parse_request(self):
        p = proxy.parse_msg(request("device/edit", {"2": DEV, "7": "716"}))
        self.assertEqual(p["type"], "req")
        self.assertEqual(p["res_act"], "device/edit")
        self.assertTrue(p["data"]["1"].startswith(MAC + "#"))
        self.assertEqual(p["data"]["7"], "716")

    def test_parse_response(self):
        msg = proxy.http_json({"code": "2", "data": {"100": proxy.watts_encode("1793829069")}})
        p = proxy.parse_msg(msg)
        self.assertEqual(p["type"], "resp")
        self.assertTrue(p["status"].startswith("200"))
        self.assertTrue(p["json"]["data"]["100"].startswith("1793829069"))
        self.assertEqual(proxy.parse_msg(b"garbage")["type"], "other")

    def test_split_http(self):
        a, b = request("query/check", {}), request("smarthome/edit", {"6": "03.63"})
        buf = bytearray(a + b[:20])
        self.assertEqual(proxy.split_http(buf), [a])
        buf += b[20:]
        self.assertEqual(proxy.split_http(buf), [b])
        self.assertEqual(buf, bytearray())
        resp = proxy.http_json({"code": "8", "data": {}})
        buf = bytearray(resp[:-3])
        self.assertEqual(proxy.split_http(buf), [])            # body incomplete
        buf += resp[-3:]
        self.assertEqual(proxy.split_http(buf), [resp])

    def test_local_answers(self):
        def ans(res_act):
            return body_of(proxy.local_answer({"type": "req", "res_act": res_act}))
        self.assertEqual(ans("query/check")["code"], "2")
        self.assertEqual(ans("device/edit")["code"], "8")
        self.assertEqual(ans("stats/insert")["code"], "8")
        self.assertEqual(ans("sandbox/get_current_timestamp")["code"], "1")
        self.assertIn("14", ans("smarthome/read_mode")["data"])
        ts = int(proxy.watts_decode(ans("query/check")["data"]["100"]))
        self.assertLess(abs(ts - time.time()), 5)

    def test_local_answer_from_cache(self):
        resp = proxy.http_json({"code": "1", "data": {"100": "x", "5": "abc"}})
        proxy.remember_cloud_answer("smarthome/read", resp)
        self.assertEqual(body_of(proxy.local_answer({"type": "req", "res_act": "smarthome/read"}))["data"]["5"], "abc")
        # Answers with a command are never cached.
        proxy.remember_cloud_answer("smarthome/read2", proxy.http_json({"code": "1", "data": {"3": {"2": DEV}}}))
        self.assertNotIn("smarthome/read2", proxy.CACHE)

    def test_read_chunked_response(self):
        async def run():
            r = asyncio.StreamReader()
            r.feed_data(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
            return await proxy.read_http_response(r)
        msg, close = asyncio.run(run())
        self.assertTrue(msg.endswith(b"Content-Length: 11\r\n\r\nhello world"))
        self.assertFalse(close)


class FakeUp:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def exchange(self, req):
        self.calls += 1
        if self.fail:
            raise ConnectionError("cloud down")
        return proxy.http_json({"code": "8", "data": {"100": proxy.watts_encode("1")}})


class Answer(unittest.TestCase):
    def setUp(self):
        proxy.WATTS = make_state()
        proxy.CLOUD.update(down_until=0.0, local=False)
        proxy.OPT.update(force_local=False, local_fallback=True, local_delay_ms=0)

    def run_answer(self, up, msg=None):
        msg = msg or request("device/edit", {"2": DEV})
        return asyncio.run(proxy.answer(up, msg, proxy.parse_msg(msg)))

    def test_cloud_ok(self):
        _, origin = self.run_answer(FakeUp())
        self.assertEqual(origin, "cloud->central")
        self.assertEqual(proxy.WATTS.connection, "cloud")

    def test_cloud_down_answers_locally_and_waits(self):
        up = FakeUp(fail=True)
        resp, origin = self.run_answer(up)
        self.assertEqual(origin, "local->central")
        self.assertEqual(body_of(resp)["code"], "8")
        self.assertEqual(proxy.WATTS.connection, "local")
        self.run_answer(up)
        self.assertEqual(up.calls, 1)                           # no retry before cloud_retry_seconds
        self.assertEqual(proxy.WATTS.mqtt.events()[-1]["event_type"], "cloud_unavailable")

    def test_no_fallback(self):
        proxy.OPT["local_fallback"] = False
        resp, _ = self.run_answer(FakeUp(fail=True))
        self.assertIsNone(resp)

    def test_forced_local(self):
        proxy.OPT["force_local"] = True
        up = FakeUp()
        _, origin = self.run_answer(up)
        self.assertEqual((origin, up.calls), ("local->central", 0))

    def test_not_watts(self):
        resp, origin = self.run_answer(FakeUp(), b"PROPFIND / HTTP/1.1\r\n\r\n")
        self.assertTrue(resp.startswith(b"HTTP/1.1 404"))


class EndToEnd(unittest.TestCase):
    """Fake central unit -> proxy -> fake cloud, over real sockets on localhost."""

    def test_flow(self):
        async def run():
            cloud_log = []
            cloud_state = {"up": True}

            async def cloud(reader, writer):
                while True:
                    try:
                        head = await reader.readuntil(b"\r\n\r\n")
                    except (asyncio.IncompleteReadError, ConnectionError):
                        break
                    if not cloud_state["up"]:
                        writer.close()
                        return
                    cloud_log.append(proxy.parse_msg(head)["res_act"])
                    code = "2" if b"query/check" in head else "8"
                    writer.write(proxy.http_json({"code": code, "data": {"100": proxy.watts_encode("1")}}))
                    await writer.drain()
                writer.close()

            cloud_srv = await asyncio.start_server(cloud, "127.0.0.1", 0)
            proxy._dns_cache.update(ip="127.0.0.1", expires=time.time() + 3600)
            proxy.OPT.update(upstream_port=cloud_srv.sockets[0].getsockname()[1], local_delay_ms=0,
                             force_local=False, local_fallback=True, cloud_retry_seconds=60)
            proxy.CLOUD.update(down_until=0.0, local=False)
            proxy.WATTS = make_state()
            srv = await asyncio.start_server(proxy.handle_http, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def send(res_act, data):
                writer.write(request(res_act, data))
                await writer.drain()
                msg, _ = await proxy.read_http_response(reader)
                return body_of(msg)

            # 1. Cloud up: forwarded, and the thermostat appears in HA.
            self.assertEqual((await send("device/edit", device_fields()))["code"], "8")
            self.assertEqual(cloud_log, ["device/edit"])
            self.assertIn(DEV, proxy.WATTS.devices)
            # 2. A command from HA is delivered in place of "no commands".
            proxy.WATTS._on_command("watts_proxy/c002_001/set/preset", "eco")
            got = await send("query/check", {})
            self.assertEqual((got["code"], got["data"]["3"]["22"]), ("1", "3"))
            # 3. Cloud down: local answers, the central unit never notices.
            cloud_state["up"] = False
            self.assertEqual((await send("device/edit", device_fields(mode="3")))["code"], "8")
            self.assertEqual(proxy.WATTS.connection, "local")
            self.assertNotIn("C002-001|mode", proxy.WATTS.pending)    # eco confirmed while local
            writer.close()
            srv.close()
            cloud_srv.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
