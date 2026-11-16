"""Watts Proxy: local proxy between a Watts BT-CT02-RF central unit and the Watts cloud.

The central unit talks plain HTTP on port 80 to smarthome.wattselectronics.com. With a
DNS rewrite (e.g. AdGuard Home) that name points to this add-on, which:

- forwards every request to the real cloud and returns its answer, so the Watts app
  keeps working;
- answers by itself, with the same format as the cloud, when the cloud does not answer
  (local mode), and tries the cloud again every cloud_retry_seconds;
- publishes the thermostats to Home Assistant over MQTT (see watts_ha.py) and, with
  allow_control, delivers commands from Home Assistant in both modes.

Logs: /share/watts_proxy/YYYY-MM-DD.log (detail) and the add-on log (summary).
"""

import asyncio
import datetime as dt
import email.utils
import json
import os
import random
import re
import socket
import struct
import time
import traceback
import urllib.parse
import urllib.request

import watts_ha

OPTIONS_FILE = os.environ.get("WP_OPTIONS", "/data/options.json")
LOG_DIR = os.environ.get("WP_LOG_DIR", "/share/watts_proxy")
DATA_DIR = os.environ.get("WP_DATA", "/data")
VERSION = "0.9.0"

DEFAULTS = {
    "language": "en",
    "upstream_host": "smarthome.wattselectronics.com",
    "upstream_dns": "1.1.1.1",
    "upstream_ip_fallback": "108.142.40.45",
    "listen_http": 80,
    "mqtt_enabled": True,
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_password": "",
    "discovery_prefix": "homeassistant",
    "allow_control": False,
    "local_fallback": True,
    "force_local": False,
    "cloud_timeout": 5,
    "cloud_retry_seconds": 60,
    "local_delay_ms": 120,
    "central_timeout_min": 5,
    "zone_timeout_min": 30,
    "timezone": "",
    "log_routine": False,
    "full_capture_connections": 0,
    "max_payload_bytes": 16384,
}


def load_options():
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            opts.update({k: v for k, v in json.load(f).items() if v is not None})
    except FileNotFoundError:
        pass
    return opts


OPT = load_options()

# Time zone: the Supervisor passes TZ to add-ons; the option overrides it. It matters for
# the logs and, above all, for the current program period (comfort/eco), which uses local time.
if OPT.get("timezone"):
    os.environ["TZ"] = OPT["timezone"]
if hasattr(time, "tzset"):
    time.tzset()
conn_counter = 0


# --------------------------------------------------------------------------- log

def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def summary(msg, routine=False):
    if routine and not OPT.get("log_routine"):
        return
    print(f"{now()} {msg}", flush=True)
    write_file(f"{now()} {msg}\n")


def write_file(text):
    try:
        path = os.path.join(LOG_DIR, dt.date.today().isoformat() + ".log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def render(data: bytes) -> str:
    """Text if almost everything is printable; hex dump otherwise."""
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    if data and printable / len(data) > 0.9:
        return data.decode("latin-1")
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:06x}  {hx:<47}  {asc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- MQTT broker

def resolve_mqtt(opts):
    """Without mqtt_host, take the broker and its credentials from the Supervisor (Mosquitto add-on)."""
    if not opts.get("mqtt_enabled") or opts.get("mqtt_host"):
        return
    token = os.environ.get("SUPERVISOR_TOKEN")
    opts["mqtt_host"] = "127.0.0.1"
    if not token:
        summary("[mqtt] no mqtt_host and no Supervisor: using 127.0.0.1")
        return
    try:
        req = urllib.request.Request("http://supervisor/services/mqtt",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)["data"]
    except Exception as e:  # noqa: BLE001
        summary(f"[mqtt] could not get the broker from the Supervisor ({type(e).__name__}: {e}); "
                "using 127.0.0.1. Install the Mosquitto add-on or set mqtt_host.")
        return
    host = d.get("host") or "127.0.0.1"
    try:
        socket.gethostbyname(host)
    except OSError:
        host = "127.0.0.1"      # with host networking, core-mosquitto may not resolve
    opts.update(mqtt_host=host, mqtt_port=int(d.get("port") or 1883),
                mqtt_user=d.get("username") or "", mqtt_password=d.get("password") or "")
    summary(f"[mqtt] broker from the Supervisor: {host}:{opts['mqtt_port']}")


# --------------------------------------------------------------------------- DNS

_dns_cache = {"ip": None, "expires": 0.0}


def _dns_query_a(name: str, server: str, timeout=3.0):
    """Minimal A query over UDP, sent straight to the given DNS server (bypassing the rewrite)."""
    qid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server, 53))
        resp, _ = s.recvfrom(2048)
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", resp[:12])
    if rid != qid or (flags & 0x000F) != 0:
        raise RuntimeError(f"invalid DNS answer (rcode={flags & 0xF})")
    pos = 12
    for _ in range(qd):
        while resp[pos] != 0:
            if resp[pos] & 0xC0 == 0xC0:
                pos += 1
                break
            pos += resp[pos] + 1
        pos += 1 + 4
    for _ in range(an):
        if resp[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while resp[pos] != 0:
                pos += resp[pos] + 1
            pos += 1
        rtype, _, ttl, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            return socket.inet_ntoa(resp[pos:pos + 4]), ttl
        pos += rdlen
    raise RuntimeError("no A record")


async def upstream_ip():
    if _dns_cache["ip"] and time.time() < _dns_cache["expires"]:
        return _dns_cache["ip"]
    try:
        ip, ttl = await asyncio.get_running_loop().run_in_executor(
            None, _dns_query_a, OPT["upstream_host"], OPT["upstream_dns"])
        _dns_cache.update(ip=ip, expires=time.time() + max(30, min(ttl, 3600)))
    except Exception as e:  # noqa: BLE001
        ip = _dns_cache["ip"] or OPT["upstream_ip_fallback"]
        # The failure is cached too, so that each connection does not wait for the timeout.
        _dns_cache.update(ip=ip, expires=time.time() + 120)
        summary(f"[dns] could not resolve {OPT['upstream_host']} via {OPT['upstream_dns']}: {e}; "
                f"using {ip} (retry in 2 min)")
    return ip


# --------------------------------------------------------------------------- Watts protocol

WATTS_KEY = "92533220362375"


def watts_encode(s: str) -> str:
    return "".join(chr(ord(c) + ord(WATTS_KEY[i % len(WATTS_KEY)])) for i, c in enumerate(s))


def watts_decode(s: str) -> str:
    """Undo the Watts obfuscation: ord(key[i % 14]) was added to each character."""
    return "".join(chr(ord(c) - ord(WATTS_KEY[i % len(WATTS_KEY)])) for i, c in enumerate(s))


def parse_msg(msg: bytes):
    """Parse a complete Watts HTTP message, with the obfuscated fields decoded.

    Returns a dict with "type" = "req" (res_act, data), "resp" (status, json, text)
    or "other" (line).
    """
    head, _, body = msg.partition(b"\r\n\r\n")
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    try:
        if line.startswith("GET ") and "/api/v0.1/machine/" in line:
            url = line.split(" ")[1]
            path = url.split("/api/v0.1/machine/", 1)[1]
            res, act, payload = path.split("/", 2)
            data = json.loads(urllib.parse.unquote(payload))
            if isinstance(data.get("1"), str):
                data["1"] = watts_decode(data["1"])
            return {"type": "req", "res_act": f"{res}/{act}", "data": data}
        if line.startswith("HTTP/"):
            text = body.decode("utf-8", "replace")
            try:
                j = json.loads(text)
                d = j.get("data") if isinstance(j, dict) else None
                if isinstance(d, dict) and isinstance(d.get("100"), str):
                    ts = int(watts_decode(d["100"]))
                    d["100"] = f"{ts} ({dt.datetime.fromtimestamp(ts, dt.timezone.utc):%H:%M:%S}Z)"
            except (ValueError, TypeError):
                j = None
            return {"type": "resp", "status": line.split(" ", 1)[1], "json": j, "text": text[:300]}
    except (IndexError, ValueError):
        pass
    return {"type": "other", "line": line[:300]}


def describe(p) -> str:
    if p["type"] == "req":
        return f"{p['res_act']} {json.dumps(p['data'], ensure_ascii=False)}"
    if p["type"] == "resp":
        body = json.dumps(p["json"], ensure_ascii=False) if p["json"] is not None else p["text"]
        return f"{p['status']} {body}"
    return p["line"]


def split_http(buf: bytearray):
    """Take the complete HTTP messages (headers + Content-Length) out of the buffer."""
    msgs = []
    while True:
        end = buf.find(b"\r\n\r\n")
        if end < 0:
            break
        head = bytes(buf[:end]).lower()
        cl = 0
        for h in head.split(b"\r\n"):
            if h.startswith(b"content-length:"):
                try:
                    cl = int(h.split(b":", 1)[1])
                except ValueError:
                    cl = 0
        total = end + 4 + cl
        if len(buf) < total:
            break
        msgs.append(bytes(buf[:total]))
        del buf[:total]
    return msgs


# --------------------------------------------------------------------------- state

WATTS = None            # watts_ha.WattsState, created in main()


def handle_watts(p):
    """Pass a parsed message to the HA state. Returns True for routine traffic."""
    if p["type"] == "req":
        WATTS.on_request(p["res_act"], p["data"])
        return True
    if p["type"] == "resp":
        WATTS.on_response(p["json"] or {})
        return not (isinstance(p["json"], dict) and p["json"].get("code") == "1")
    return False


def interpret(cid, direction, msg, ctx):
    p = parse_msg(msg)
    if p["type"] == "req":
        ctx["last_req"] = p["res_act"]
    try:
        routine = handle_watts(p)
    except Exception as e:  # noqa: BLE001
        routine = False
        summary(f"[{cid}] error interpreting: {type(e).__name__}: {e}\n" + traceback.format_exc())
    summary(f"[{cid}] {direction} {describe(p)}", routine=routine)
    return p


def new_conn(port, peer):
    global conn_counter
    conn_counter += 1
    cid = f"{conn_counter:06d}"
    capture = conn_counter <= int(OPT.get("full_capture_connections") or 0)
    summary(f"[{cid}] new connection :{port} from {peer[0]}:{peer[1]}", routine=True)
    return cid, capture


# --------------------------------------------------------------------------- cloud / local mode

CLOUD = {"down_until": 0.0, "local": False}
CACHE_FILE = watts_ha.data_file(DATA_DIR, "cloud_responses.json", "respuestas_nube.json")
FORCE_FILE = watts_ha.data_file(DATA_DIR, "force_local.json", "forzar_local.json")
IDLE_TIMEOUT = 75          # s without requests before closing the connection with the central unit
HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"HEAD ", b"DELETE ", b"OPTIONS ")
# smarthome/read_mode answer if the cloud's was never seen (from Fl0kkie's server).
DEFAULT_READ_MODE = {
    "14": {"0": {"25": "1", "26": "0", "27": "C"}, "1": {"25": "1", "26": "14", "27": "E"},
           "2": {"25": "1", "26": "14", "27": "O"}},
    "15": {"0": {"25": "2", "26": "14", "27": "C"}, "1": {"25": "2", "26": "14", "27": "E"},
           "2": {"25": "2", "26": "14", "27": "O"}},
}


def _load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CACHE = _load_cache()      # res_act -> last valid cloud answer (without "100")


def remember_cloud_answer(res_act, resp: bytes):
    """Keep the cloud's answer to imitate it in local mode.

    query/check and anything carrying a command are never kept: in local mode only
    commands from Home Assistant are delivered.
    """
    if not res_act or res_act == "query/check":
        return
    try:
        j = json.loads(resp.partition(b"\r\n\r\n")[2].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict) or "3" in data:
        return
    entry = {"code": j.get("code"), "data": {k: v for k, v in data.items() if k != "100"}}
    if CACHE.get(res_act) != entry:
        CACHE[res_act] = entry
        try:
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(CACHE, f, ensure_ascii=False, indent=1)
            os.replace(tmp, CACHE_FILE)
        except OSError as e:
            summary(f"[local] could not save {CACHE_FILE}: {e}")


def http_json(obj) -> bytes:
    """Local answer with the same headers, in the same order, as the real cloud
    (above all Date, which the central unit seems to expect)."""
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    head = ("HTTP/1.1 200 OK\r\n"
            f"Date: {email.utils.formatdate(usegmt=True)}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: keep-alive\r\n"
            f"K8S-POD-ID: {CLOUD.get('pod') or 'machine-api-7c84b68495-zcbls'}\r\n"
            "Cache-Control: no-cache, no-store, max-age=0, must-revalidate\r\n"
            "Pragma: no-cache\r\n"
            "Expires: 0\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "X-Frame-Options: DENY\r\n"
            "X-XSS-Protection: 0\r\n"
            "Referrer-Policy: no-referrer\r\n"
            f"Content-Length: {len(body)}\r\n\r\n")
    return head.encode("ascii") + body


def local_answer(p) -> bytes:
    """The answer the cloud would give, generated by the proxy."""
    res_act = p.get("res_act", "") if p["type"] == "req" else ""
    token = {"100": watts_encode(str(int(time.time())))}
    act = res_act.split("/")[-1]
    if res_act == "query/check":
        obj = {"code": "2", "data": token}           # no commands (HA commands are injected later)
    elif res_act in CACHE:
        c = CACHE[res_act]
        obj = {"code": c["code"], "data": {**token, **c["data"]}}
    elif res_act == "sandbox/get_current_timestamp":
        obj = {"code": "1", "data": token}
    elif res_act == "smarthome/read_mode":
        obj = {"code": "1", "data": {**token, **DEFAULT_READ_MODE}}
    elif act.startswith("read"):
        obj = {"code": "1", "data": token}
    elif act.startswith(("edit", "insert")):
        obj = {"code": "8", "data": token}
    else:
        obj = {"code": "2", "data": token}
    return http_json(obj)


async def read_http_response(reader):
    """Read a complete HTTP response. Returns (message with Content-Length, close)."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head[:-4].split(b"\r\n")
    hdrs = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        hdrs[k.strip().lower()] = v.strip().lower()
    close = hdrs.get(b"connection") == b"close"
    if b"chunked" in hdrs.get(b"transfer-encoding", b""):
        body = bytearray()
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0].strip(), 16)
            if size == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":
                    pass
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
        body = bytes(body)
    elif b"content-length" in hdrs:
        body = await reader.readexactly(int(hdrs[b"content-length"]))
    elif lines[0].split(b" ")[1:2] in ([b"204"], [b"304"]):
        body = b""
    else:
        body, close = await reader.read(), True
    keep = [ln for ln in lines if not ln.lower().startswith((b"content-length:", b"transfer-encoding:"))]
    keep.append(b"Content-Length: " + str(len(body)).encode())
    return b"\r\n".join(keep) + b"\r\n\r\n" + body, close


class Upstream:
    """Keep-alive connection with the cloud, opened on demand."""

    def __init__(self):
        self.r = self.w = None

    def close(self):
        if self.w:
            self.w.close()
        self.r = self.w = None

    async def exchange(self, req: bytes) -> bytes:
        t = float(OPT["cloud_timeout"])
        for attempt in (1, 2):
            reused = self.w is not None
            try:
                if not reused:
                    ip = await upstream_ip()
                    self.r, self.w = await asyncio.wait_for(
                        asyncio.open_connection(ip, int(OPT.get("upstream_port") or 80)), t)
                self.w.write(req)
                await self.w.drain()
                resp, close = await asyncio.wait_for(read_http_response(self.r), t)
                if close:
                    self.close()
                return resp
            except (asyncio.IncompleteReadError, ConnectionError):
                self.close()
                if reused and attempt == 1:
                    continue      # the cloud closed the idle connection: retry with a new one
                raise
            except BaseException:
                self.close()
                raise
        raise ConnectionError("no answer")


def set_force_local(on: bool, source: str):
    """Turn forced local mode on/off at runtime (no restart) and remember it."""
    OPT["force_local"] = on
    try:
        with open(FORCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"force_local": on, "source": source}, f)
    except OSError as e:
        summary(f"[local] could not save {FORCE_FILE}: {e}")
    if on:
        CLOUD["local"] = True
        WATTS.set_connection(True, f"forced local mode ({source})")
    else:
        CLOUD["down_until"] = 0.0          # the next request tries the cloud again
    WATTS.set_force_local(on)
    summary(f"[local] forced local mode {'ON' if on else 'off'} ({source})")


def cloud_failed(reason):
    CLOUD["down_until"] = time.time() + float(OPT["cloud_retry_seconds"])
    if not CLOUD["local"]:
        CLOUD["local"] = True
        WATTS.set_connection(True, reason)


def cloud_ok():
    if CLOUD["local"]:
        CLOUD["local"] = False
        WATTS.set_connection(False)


def remember_pod(resp: bytes):
    m = re.search(rb"\r\nK8S-POD-ID:\s*([A-Za-z0-9._-]+)", resp[:2048], re.I)
    if m:
        CLOUD["pod"] = m.group(1).decode("ascii")


async def answer(up, req: bytes, p):
    """Answer for the central unit: from the cloud if possible, local otherwise. Returns (message, origin)."""
    if p.get("type") != "req":
        # Not the Watts API (e.g. a PC probing port 80): refused without touching the cloud
        # or the cloud/local state.
        return (b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                "proxy->other")
    if not OPT["force_local"] and time.time() >= CLOUD["down_until"]:
        try:
            resp = await up.exchange(req)
            r = parse_msg(resp)
            if r["type"] == "resp" and r["status"].startswith("200") and isinstance(r["json"], dict) \
                    and "code" in r["json"]:
                cloud_ok()
                remember_pod(resp)
                remember_cloud_answer(p.get("res_act"), resp)
                return resp, "cloud->central"
            reason = f"invalid answer: {describe(r)[:120]}"
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        cloud_failed(reason)
        if not OPT["local_fallback"]:
            return None, reason
    elif not OPT["force_local"] and not OPT["local_fallback"]:
        return None, "cloud down recently"
    delay = float(OPT.get("local_delay_ms") or 0) / 1000
    if delay > 0:
        await asyncio.sleep(delay)       # the cloud takes ~110-150 ms; imitate it
    return local_answer(p), "local->central"


async def relay_raw(cid, c_reader, c_writer, first):
    """Something that is not HTTP on port 80: copied byte by byte to the cloud and back."""
    try:
        u_reader, u_writer = await asyncio.wait_for(
            asyncio.open_connection(await upstream_ip(), int(OPT.get("upstream_port") or 80)), 10)
    except Exception as e:  # noqa: BLE001
        summary(f"[{cid}] could not connect to the cloud: {e}")
        c_writer.close()
        return

    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    u_writer.write(first)
    await u_writer.drain()
    await asyncio.gather(pipe(c_reader, u_writer), pipe(u_reader, c_writer))


async def handle_http(c_reader, c_writer):
    cid, capture = new_conn(OPT["listen_http"], c_writer.get_extra_info("peername"))
    try:
        first = await c_reader.read(65536)
    except (ConnectionError, OSError):
        first = b""
    if not first:
        c_writer.close()
        return
    if not first.startswith(HTTP_METHODS):
        await relay_raw(cid, c_reader, c_writer, first)
        return

    up, ctx, buf = Upstream(), {}, bytearray(first)
    stats = {"requests": 0, "local": 0}
    t0 = time.time()
    logged = 0

    def cap(direction, msg):
        nonlocal logged
        if capture and logged < OPT["max_payload_bytes"]:
            part = msg[:OPT["max_payload_bytes"] - logged]
            logged += len(part)
            write_file(f"--- [{cid}] {now()} {direction} {len(msg)} bytes ---\n{render(part)}\n")

    try:
        while True:
            for req in split_http(buf):
                stats["requests"] += 1
                cap("central->cloud", req)
                # Whatever happens in the proxy, the central unit gets an answer: without one,
                # it considers the server down and moves to the secondary DNS.
                try:
                    p = interpret(cid, "central->cloud", req, ctx)
                    resp, origin = await answer(up, req, p)
                    if resp is None:
                        summary(f"[{cid}] cloud unavailable ({origin}) and local_fallback off: closing")
                        return
                    if origin == "local->central":
                        stats["local"] += 1
                    cap(origin, resp)
                    interpret(cid, origin, resp, ctx)
                    out = resp
                    try:
                        out = WATTS.transform_response(resp, ctx.get("last_req"))
                    except Exception:  # noqa: BLE001
                        summary(f"[{cid}] ERROR injecting a command from HA (answering without it):\n"
                                + traceback.format_exc())
                    if out is not resp:
                        summary(f"[{cid}] proxy->central (replaced) {describe(parse_msg(out))}")
                except (ConnectionError, asyncio.IncompleteReadError):
                    raise
                except Exception:  # noqa: BLE001
                    summary(f"[{cid}] ERROR processing the request (answering locally):\n"
                            + traceback.format_exc())
                    try:
                        out = local_answer(parse_msg(req))
                    except Exception:  # noqa: BLE001
                        out = http_json({"code": "2", "data": {"100": watts_encode(str(int(time.time())))}})
                c_writer.write(out)
                await c_writer.drain()
            if len(buf) > 262144:
                buf.clear()
            try:
                # Like nginx (keepalive_timeout): an idle connection is closed.
                data = await asyncio.wait_for(c_reader.read(65536), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            buf += data
    except (ConnectionError, asyncio.IncompleteReadError, OSError) as e:
        summary(f"[{cid}] connection with the central unit lost: {type(e).__name__}: {e}")
    finally:
        up.close()
        c_writer.close()
        summary(f"[{cid}] end: {stats['requests']} requests ({stats['local']} local), "
                f"{time.time() - t0:.1f} s", routine=True)


async def main():
    global WATTS
    os.makedirs(LOG_DIR, exist_ok=True)
    resolve_mqtt(OPT)
    safe = {k: ("***" if k == "mqtt_password" and v else v) for k, v in OPT.items()}
    summary(f"[start] Watts Proxy {VERSION} options={safe}")
    summary(f"[start] time zone {os.environ.get('TZ', '(no TZ)')}: local time "
            f"{dt.datetime.now():%H:%M}, UTC {dt.datetime.now(dt.timezone.utc):%H:%M}")
    WATTS = watts_ha.WattsState(OPT, summary, LOG_DIR, DATA_DIR)
    WATTS.start()
    WATTS.force_local_handler = set_force_local
    saved = False
    try:
        with open(FORCE_FILE, encoding="utf-8") as f:
            saved = bool(json.load(f).get("force_local"))
    except (OSError, ValueError):
        pass
    if OPT["force_local"] or saved:
        set_force_local(True, "force_local option" if OPT["force_local"] else "saved state")
        summary("[start] forced local mode: the cloud is NOT used; the proxy answers the central unit")
    summary(f"[start] cloud = {await upstream_ip()}")
    server = await asyncio.start_server(handle_http, "0.0.0.0", OPT["listen_http"])
    summary(f"[start] listening on port {OPT['listen_http']}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
