# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Publish OpenKore's XKore 2 ports to nostalRE over a Cloudflare tunnel.

Speaks the same protocol as Ragnarok Offline's sharing gateway, so nostalRE's `invite` support
works unchanged:
  POST /_friend/exchange {"invite": TOKEN}  -> session cookie
  GET  /ws/<host>:<port> (WebSocket)        -> raw TCP to OpenKore at KORE_HOST:<port>
  GET  /                                    -> kore-panel.html (bot control panel)
  GET  /kore/status                         -> OPENKORE_DIR/logs/status.json (session required)
  GET  /kore/console (WebSocket)            -> OpenKore Bus console at OPENKORE_DIR/logs/console.socket

Only OpenKore's account/char/map ports are reachable. The invitation link is printed at start and
kept in kore-gateway.invite so it survives restarts.

usage: uv run kore-gateway.py [selftest]   (env: KORE_PUBLIC_HOST, KORE_HOST, KORE_PORTS, PORT, OPENKORE_DIR)
"""

import asyncio
import gzip
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import sys
import time
import zlib
from pathlib import Path

from aiohttp import WSMsgType, web

PUBLIC_HOST = os.environ.get("KORE_PUBLIC_HOST", "kore.namike.me")
KORE_HOST = os.environ.get("KORE_HOST", "127.0.0.1")
KORE_PORTS = {int(p) for p in os.environ.get("KORE_PORTS", "6901,6902,6903").split(",")}
PORT = int(os.environ.get("PORT", "8790"))
COOKIE = "__Host-ro-friend"
OPENKORE_DIR = Path(os.environ.get("OPENKORE_DIR", Path(__file__).resolve().parent.parent))
PANEL = Path(__file__).with_name("kore-panel.html")
ORIGIN = f"https://{PUBLIC_HOST}"

invite_file = Path(__file__).with_name("kore-gateway.invite")
if not invite_file.exists():
    invite_file.write_text(secrets.token_urlsafe(32))
    invite_file.chmod(0o600)
INVITE = invite_file.read_text().strip()
sessions: set[str] = set()  # ponytail: in memory; a restart makes clients re-exchange, which nostalRE does


async def exchange(request: web.Request) -> web.Response:
    if request.headers.get("Origin") != ORIGIN:
        return web.json_response({"error": "Open the original invitation link"}, status=403)
    try:
        invite = (await request.json()).get("invite", "")
    except Exception:
        return web.json_response({"error": "Invalid invitation"}, status=400)
    if not isinstance(invite, str) or not hmac.compare_digest(invite, INVITE):
        return web.json_response({"error": "This invitation is not valid"}, status=403)
    if len(sessions) > 64:
        sessions.clear()
    value = secrets.token_urlsafe(32)
    sessions.add(value)
    response = web.json_response({"ok": True})
    response.headers["Set-Cookie"] = f"{COOKIE}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=31536000"
    return response


async def socket(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Origin") != ORIGIN or request.cookies.get(COOKIE) not in sessions:
        return web.Response(status=403)
    try:
        port = int(request.match_info["target"].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return web.Response(status=400)
    if port not in KORE_PORTS:
        return web.Response(status=403)
    try:
        reader, writer = await asyncio.open_connection(KORE_HOST, port)
    except OSError:
        return web.Response(status=502)
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)

    async def upstream() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type == WSMsgType.TEXT:
                writer.write(msg.data.encode())
                await writer.drain()
            else:
                break

    async def downstream() -> None:
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    writer.close()
    await ws.close()
    return ws


# --- OpenKore control panel -------------------------------------------------------------------
# Bus protocol (openkore/src/Bus/Messages.pm): uint32 BE total length | uint8 options (0 = map) |
# uint8 id_len | id | { uint8 key_len | key | uint8 type (0 bin, 1 utf8, 2 uint32) | uint24 len | value }*


def bus_encode(mid: str, args: dict | None = None) -> bytes:
    body = bytearray()
    mid_b = mid.encode()
    body += struct.pack(">BB", 0, len(mid_b)) + mid_b
    for key, value in (args or {}).items():
        key_b = key.encode()
        if isinstance(value, int) and not isinstance(value, bool):
            vtype, data = 2, struct.pack(">I", value)
        elif isinstance(value, bytes):
            vtype, data = 0, value
        else:
            vtype, data = 1, str(value).encode()
        body += struct.pack(">B", len(key_b)) + key_b + struct.pack(">B", vtype) + len(data).to_bytes(3, "big") + data
    return struct.pack(">I", len(body) + 4) + bytes(body)


def bus_decode(buf: bytearray) -> tuple[str, dict] | None:
    """Pop one complete message from buf; None if incomplete. Array messages decode to {"items": [...]}."""
    if len(buf) < 4:
        return None
    total = struct.unpack_from(">I", buf)[0]
    if total < 6:
        raise ValueError("bad bus message length")
    if len(buf) < total:
        return None
    msg = bytes(buf[:total])
    del buf[:total]
    options, id_len = msg[4], msg[5]
    mid = msg[6:6 + id_len].decode("utf-8", "replace")
    off = 6 + id_len

    def value(vtype: int, data: bytes):
        if vtype == 2:
            return struct.unpack(">I", data.rjust(4, b"\0")[-4:])[0]
        return data.decode("utf-8", "replace")

    args: dict = {}
    items: list = []
    while off < total:
        if options == 0:
            klen = msg[off]
            key = msg[off + 1:off + 1 + klen].decode("utf-8", "replace")
            off += 1 + klen
        vtype = msg[off]
        vlen = int.from_bytes(msg[off + 1:off + 4], "big")
        data = msg[off + 4:off + 4 + vlen]
        off += 4 + vlen
        if options == 0:
            args[key] = value(vtype, data)
        else:
            items.append(value(vtype, data))
    return mid, (args if options == 0 else {"items": items})


def authed(request: web.Request) -> bool:
    return request.cookies.get(COOKIE) in sessions


async def panel(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(PANEL, headers={"Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8"})


async def status(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        data = json.loads((OPENKORE_DIR / "logs" / "status.json").read_text())
    except (OSError, ValueError):
        data = {"state": "offline"}
    return web.json_response(data, headers={"Cache-Control": "no-store"})


# ---- config editor -------------------------------------------------------------------------
# Edits control/config.txt in place. Settings mode changes top-level `key value` lines only; raw
# mode replaces the whole file. Credentials never leave the Mac: their values are masked on read
# and the masked line is restored from disk on write, and the web can't change them.
CONFIG = OPENKORE_DIR / "control" / "config.txt"
BACKUPS = OPENKORE_DIR / "control" / "backups"
MASK = "********"
SECRET = re.compile(r"password|passwd|pincode|username|otpseed|secret|token", re.I)
config_lock = asyncio.Lock()


def config_lines(text: str) -> list[tuple[int, str, str]]:
    """(line index, key, value) for every top-level setting, outside `key {` ... `}` blocks."""
    out, depth = [], 0
    for i, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("{"):
            depth += 1
        elif line == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            key, _, value = line.partition(" ")
            out.append((i, key, value.strip()))
    return out


def mask(text: str) -> str:
    lines = text.split("\n")
    for i, key, value in config_lines(text):
        if SECRET.search(key) and value:
            lines[i] = f"{key} {MASK}"
    return "\n".join(lines)


def unmask(new: str, old: str) -> str:
    """Puts the real credential lines back; refuses a credential the web tried to set."""
    real = {key: value for _, key, value in config_lines(old) if SECRET.search(key)}
    lines = new.split("\n")
    for i, key, value in config_lines(new):
        if not SECRET.search(key):
            continue
        if value == MASK:
            lines[i] = f"{key} {real.get(key, '')}".rstrip()
        elif value != real.get(key, ""):
            raise ValueError(f"{key} can only be changed on the Mac")
    return "\n".join(lines)


def check_raw(text: str) -> None:
    depth = 0
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.endswith("{"):
            depth += 1
        elif line == "}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"line {n}: '}}' without a block")
    if depth:
        raise ValueError("a block is missing its closing '}'")
    if len(text) > 512 * 1024:
        raise ValueError("file too large")


def backup_names(stem: str) -> list[str]:
    """Backups of control/<stem>.txt, newest first."""
    pattern = re.compile(re.escape(stem) + r"-\d{8}-\d{6}\.txt")
    if not BACKUPS.exists():
        return []
    return sorted((p.name for p in BACKUPS.iterdir() if pattern.fullmatch(p.name)), reverse=True)


def write_backed_up(path: Path, stem: str, text: str) -> str:
    """Copy path to backups/<stem>-YYYYmmdd-HHMMSS.txt (keep 20), then atomically replace it."""
    BACKUPS.mkdir(exist_ok=True)
    name = time.strftime(f"{stem}-%Y%m%d-%H%M%S.txt")
    shutil.copy2(path, BACKUPS / name)
    for old in backup_names(stem)[20:]:  # keep the last 20
        (BACKUPS / old).unlink()
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_bytes(text.encode("utf-8", "surrogateescape"))
    os.replace(tmp, path)
    return name


def write_config(text: str) -> str:
    return write_backed_up(CONFIG, "config", text)


async def kore_command(cmd: str) -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(str(OPENKORE_DIR / "logs" / "console.socket"))
    except OSError:
        return False
    writer.write(bus_encode("input", {"data": cmd}))
    await writer.drain()
    writer.close()
    return True


def read_config() -> str:
    return CONFIG.read_bytes().decode("utf-8", "surrogateescape")


async def config_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    text = read_config()
    settings = [{"key": k, "value": MASK if SECRET.search(k) and v else v, "locked": bool(SECRET.search(k))}
                for _, k, v in config_lines(text)]
    return web.json_response({"settings": settings, "raw": mask(text), "backups": backup_names("config")},
                             headers={"Cache-Control": "no-store"})


async def config_post(request: web.Request) -> web.Response:
    if request.headers.get("Origin") != ORIGIN or not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    async with config_lock:
        old = read_config()
        try:
            if isinstance(body.get("raw"), str):
                new = unmask(body["raw"].replace("\r\n", "\n"), old)
                check_raw(new)
            elif isinstance(body.get("changes"), dict):
                lines = old.split("\n")
                where = {k: i for i, k, _ in config_lines(old)}
                for key, value in body["changes"].items():
                    if key not in where:
                        raise ValueError(f"unknown setting {key}")
                    if SECRET.search(key):
                        raise ValueError(f"{key} can only be changed on the Mac")
                    if not isinstance(value, str) or len(value) > 500 or any(c in value for c in "\r\n{}"):
                        raise ValueError(f"invalid value for {key}")
                    lines[where[key]] = f"{key} {value.strip()}".rstrip()
                new = "\n".join(lines)
            elif isinstance(body.get("restore"), str) and re.fullmatch(r"config-\d{8}-\d{6}\.txt", body["restore"]):
                new = (BACKUPS / body["restore"]).read_bytes().decode("utf-8", "surrogateescape")
            else:
                raise ValueError("nothing to save")
        except (ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if new == old:
            return web.json_response({"ok": True, "unchanged": True})
        backup = write_config(new)
    reloaded = await kore_command("reload config")
    return web.json_response({"ok": True, "backup": backup, "reloaded": reloaded})


# ---- other control files ----------------------------------------------------------------------
CONTROL = OPENKORE_DIR / "control"
CONTROL_FILES = ("mon_control.txt items_control.txt pickupitems.txt shop.txt buyer_shop.txt timeouts.txt "
                 "priority.txt routeweights.txt chat_resp.txt responses.txt avoid.txt arrowcraft.txt "
                 "consolecolors.txt").split()
MAX_TEXT = 512 * 1024


def control_file(name: str) -> Path | None:
    """The control file for an allowlisted name, else None (no paths, no config.txt)."""
    if not re.fullmatch(r"[a-z_]+\.txt", name) or name not in CONTROL_FILES:
        return None
    return CONTROL / name


async def files_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    files = [{"name": n, "size": (CONTROL / n).stat().st_size} for n in CONTROL_FILES if (CONTROL / n).is_file()]
    return web.json_response({"files": files}, headers={"Cache-Control": "no-store"})


async def file_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    name = request.match_info["name"]
    path = control_file(name)
    if path is None or not path.is_file():
        return web.json_response({"error": "no such file"}, status=404)
    text = path.read_bytes().decode("utf-8", "surrogateescape")
    return web.json_response({"name": name, "text": text, "backups": backup_names(path.stem)},
                             headers={"Cache-Control": "no-store"})


async def file_post(request: web.Request) -> web.Response:
    if request.headers.get("Origin") != ORIGIN or not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    name = request.match_info["name"]
    path = control_file(name)
    if path is None or not path.is_file():
        return web.json_response({"error": "no such file"}, status=404)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad request"}, status=400)
    stem = path.stem
    async with config_lock:
        old = path.read_bytes().decode("utf-8", "surrogateescape")
        try:
            if isinstance(body.get("text"), str):
                new = body["text"].replace("\r\n", "\n")
            elif isinstance(body.get("restore"), str) and re.fullmatch(re.escape(stem) + r"-\d{8}-\d{6}\.txt",
                                                                       body["restore"]):
                new = (BACKUPS / body["restore"]).read_bytes().decode("utf-8", "surrogateescape")
            else:
                raise ValueError("nothing to save")
            if len(new.encode("utf-8", "surrogateescape")) > MAX_TEXT:
                raise ValueError("file too large")
        except (ValueError, OSError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if new == old:
            return web.json_response({"ok": True, "unchanged": True})
        backup = write_backed_up(path, stem, new)
    reloaded = await kore_command(f"reload {stem}")
    return web.json_response({"ok": True, "backup": backup, "reloaded": reloaded})


# ---- field maps -------------------------------------------------------------------------------
# .fld2 (openkore/src/Field.pm loadFile): uint16 LE width, uint16 LE height, then width*height
# bytes, cell (x, y) at y*width + x. Bits: 1 walk, 2 snipe, 4 water, 8 cliff.
FIELDS = OPENKORE_DIR / "fields"
# palette: 0 blocked (dark), 1 walkable (light), 2 water (blue), 3 walkable water (light blue)
MAP_PALETTE = bytes((0x24, 0x26, 0x2e, 0xe6, 0xe2, 0xd6, 0x2a, 0x5c, 0xa8, 0x8c, 0xb8, 0xe6))
MAP_INDEX = bytes((1 if v & 1 else 0) + (2 if v & 4 else 0) for v in range(256))
map_cache: dict[str, tuple[bytes, int, int]] = {}


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def field_png(data: bytes) -> tuple[bytes, int, int]:
    """Palette PNG of a .fld2 payload, 1 px per cell, row 0 = highest y."""
    if len(data) < 4:
        raise ValueError("truncated field")
    w, h = struct.unpack_from("<HH", data)
    cells = data[4:4 + w * h]
    if not w or not h or len(cells) < w * h:
        raise ValueError("truncated field")
    cells = cells.translate(MAP_INDEX)
    raw = b"".join(b"\0" + cells[y * w:(y + 1) * w] for y in range(h - 1, -1, -1))
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0))
           + png_chunk(b"PLTE", MAP_PALETTE) + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b""))
    return png, w, h


def load_field_png(field: str) -> tuple[bytes, int, int] | None:
    if not re.fullmatch(r"[A-Za-z0-9@_\-.]+", field) or field.startswith("."):
        return None
    if field in map_cache:
        return map_cache[field]
    plain, packed = FIELDS / f"{field}.fld2", FIELDS / f"{field}.fld2.gz"
    if plain.is_file():
        data = plain.read_bytes()
    elif packed.is_file():
        data = gzip.decompress(packed.read_bytes())
    else:
        return None
    result = field_png(data)
    while len(map_cache) >= 50:
        map_cache.pop(next(iter(map_cache)))
    map_cache[field] = result
    return result


async def map_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        result = await asyncio.to_thread(load_field_png, request.match_info["field"])
    except (OSError, ValueError, EOFError, zlib.error):
        return web.json_response({"error": "unreadable field"}, status=500)
    if result is None:
        return web.json_response({"error": "no such field"}, status=404)
    png, w, h = result
    return web.Response(body=png, content_type="image/png",
                        headers={"X-Map-Size": f"{w}x{h}", "Cache-Control": "private, max-age=86400"})


async def console(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Origin") != ORIGIN or not authed(request):
        return web.Response(status=403)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    try:
        reader, writer = await asyncio.open_unix_connection(str(OPENKORE_DIR / "logs" / "console.socket"))
    except OSError:
        await ws.send_json({"ev": "error", "message": "OpenKore not running with --interface=Socket"})
        await ws.close()
        return ws
    writer.write(bus_encode("set active"))
    await writer.drain()

    async def upstream() -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            try:
                cmd = json.loads(msg.data).get("input")
            except (ValueError, AttributeError):
                continue
            if not isinstance(cmd, str):
                continue
            cmd = cmd.replace("\r", " ").replace("\n", " ").strip()[:500]
            if cmd:
                writer.write(bus_encode("input", {"data": cmd}))
                await writer.drain()

    async def downstream() -> None:
        buf = bytearray()
        names = {"output": "output", "title changed": "title", "inputted": "inputted"}
        while data := await reader.read(65536):
            buf += data
            while (decoded := bus_decode(buf)) is not None:
                mid, args = decoded
                if mid in names:
                    await ws.send_json({"ev": names[mid], **args})

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    writer.close()
    if not ws.closed:
        await ws.send_json({"ev": "error", "message": "OpenKore console disconnected"})
    await ws.close()
    return ws


def selftest() -> None:
    m = bus_encode("output", {"type": "error", "message": "héllo\n", "domain": "x", "n": 70000, "b": b"\xff\x00"})
    assert struct.unpack(">I", m[:4])[0] == len(m)
    buf = bytearray(m + bus_encode("set active") + m[:5])
    assert bus_decode(buf) == ("output", {"type": "error", "message": "héllo\n", "domain": "x", "n": 70000, "b": "�\x00"})
    assert bus_decode(buf) == ("set active", {})
    assert bus_decode(buf) is None and len(buf) == 5
    assert bus_encode("set active") == b"\x00\x00\x00\x10\x00\x0aset active"
    assert bus_encode("input", {"data": "ai on"}) == b"\x00\x00\x00\x19\x00\x05input\x04data\x01\x00\x00\x05ai on"
    arr = bytearray(struct.pack(">IBB", 6 + 1 + 4 + 2, 1, 1) + b"a" + b"\x01\x00\x00\x02hi")
    assert bus_decode(arr) == ("a", {"items": ["hi"]})
    cfg = "username bob\npassword hunter2\nlockMap prt_fild08\nattackSkillSlot Bash {\n\tlvl 10\n}\n"
    assert [(k, v) for _, k, v in config_lines(cfg)] == [("username", "bob"), ("password", "hunter2"), ("lockMap", "prt_fild08")]
    shown = mask(cfg)
    assert "hunter2" not in shown and "bob" not in shown
    assert unmask(shown.replace("prt_fild08", "pay_fild01"), cfg) == cfg.replace("prt_fild08", "pay_fild01")
    try:
        unmask(shown.replace(MASK, "stolen", 1), cfg)
        raise AssertionError("credential change accepted")
    except ValueError:
        pass
    check_raw(cfg)
    for bad in ("a {\n", "}\n"):
        try:
            check_raw(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert control_file("mon_control.txt") == CONTROL / "mon_control.txt"
    for bad in ("config.txt", "../config.txt", "mon_control.txt/..", "/etc/passwd", "sys.txt", "MON_CONTROL.TXT",
                "mon_control.txt\0", "..", "", "control/mon_control.txt", "mon_control.txt.bak"):
        assert control_file(bad) is None, bad
    for bad in ("../izlude", "izlude/x", "", "..", ".hidden", "a b", "iz\0lude"):
        assert load_field_png(bad) is None, bad
    # 3x2 field: y=0 row [walk, blocked, water], y=1 row [walk|water, walk, cliff]
    png, w, h = field_png(struct.pack("<HH", 3, 2) + bytes((1, 0, 4, 5, 1, 8)))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR" and struct.unpack(">II", png[16:24]) == (3, 2)
    idat_len = struct.unpack(">I", png[57:61])[0]  # IHDR (25) + PLTE (12+12) after signature
    assert png[61:65] == b"IDAT"
    assert zlib.decompress(png[65:65 + idat_len]) == b"\0\x03\x01\x00" + b"\0\x01\x00\x02"  # top row = y 1
    try:
        field_png(struct.pack("<HH", 3, 2) + b"\1")
        raise AssertionError("truncated field accepted")
    except ValueError:
        pass
    real = load_field_png("izlude")
    if real:
        hdr = gzip.decompress((FIELDS / "izlude.fld2.gz").read_bytes())[:4]
        assert struct.unpack(">II", real[0][16:24]) == struct.unpack("<HH", hdr) == (real[1], real[2])
        print(f"izlude {real[1]}x{real[2]}, {len(real[0])} bytes")
    print("selftest ok")


# ---- name tables -----------------------------------------------------------------------------
# OpenKore names items/skills from one table folder (kRO here). The panel can show another
# language: it fetches these id -> name maps and swaps names client side.
LANGS = {"en": "iRO", "zh-tw": "twRO", "zh-cn": "cRO"}
names_cache: dict[str, bytes] = {}


def name_table(path: Path) -> dict[str, str]:
    out = {}
    try:
        for line in path.read_text("utf-8", "replace").splitlines():
            parts = line.split("#")
            if len(parts) >= 2 and parts[0] and parts[1] and not line.startswith("//"):
                out[parts[0].strip()] = parts[1].strip().replace("_", " ")
    except OSError:
        pass
    return out


async def names_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    lang = request.match_info["lang"]
    if lang not in LANGS:
        return web.json_response({"error": "unknown language"}, status=404)
    if lang not in names_cache:
        # Missing names fall back to the other tables (own language first), then the panel keeps
        # OpenKore's own name.
        order = [lang] + [l for l in LANGS if l != lang]
        tables = {}
        for kind, file in (("items", "items.txt"), ("skills", "skillnametable.txt")):
            merged: dict[str, str] = {}
            for l in reversed(order):
                merged.update(name_table(OPENKORE_DIR / "tables" / LANGS[l] / file))
            tables[kind] = merged
        names_cache[lang] = json.dumps(tables, ensure_ascii=False).encode()
    return web.Response(body=names_cache[lang], content_type="application/json",
                        headers={"Cache-Control": "private, max-age=86400"})


app = web.Application()
app.router.add_post("/_friend/exchange", exchange)
app.router.add_get("/ws/{target}", socket)
app.router.add_get("/", panel)
app.router.add_get("/kore/status", status)
app.router.add_get("/kore/console", console)
app.router.add_get("/kore/config", config_get)
app.router.add_post("/kore/config", config_post)
app.router.add_get("/kore/files", files_get)
app.router.add_get("/kore/names/{lang}.json", names_get)
app.router.add_get("/kore/file/{name}", file_get)
app.router.add_post("/kore/file/{name}", file_post)
app.router.add_get("/kore/map/{field}.png", map_get)

if __name__ == "__main__" and sys.argv[1:] == ["selftest"]:
    selftest()
elif __name__ == "__main__":
    print(f"invitation: https://{PUBLIC_HOST}/#invite={INVITE}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
