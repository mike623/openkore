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
  GET  /kore/status                         -> <bot logs>/status.json + bots list (cookie kore-bot picks the bot)
  GET  /kore/models                        -> chat models from KORE_AI_URL/models
  POST /kore/chat                          -> AI advisor (tools over game data; KORE_AI_URL/MODEL)
  GET  /kore/console (WebSocket)            -> OpenKore Bus console at OPENKORE_DIR/logs/console.socket

Only OpenKore's account/char/map ports are reachable. The invitation link is printed at start and
kept in kore-gateway.invite so it survives restarts.

usage: uv run kore-gateway.py [selftest | cmd "<console command>" | ask "<question for the AI>"]   (env: KORE_PUBLIC_HOST, KORE_HOST, KORE_PORTS, PORT, OPENKORE_DIR)
"""

import asyncio
import contextvars
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

import aiohttp
from aiohttp import WSMsgType, web

PUBLIC_HOST = os.environ.get("KORE_PUBLIC_HOST", "kore.namike.me")
KORE_HOST = os.environ.get("KORE_HOST", "127.0.0.1")
KORE_PORTS = {int(p) for p in os.environ.get("KORE_PORTS", "6901,6902,6903").split(",")}
PORT = int(os.environ.get("PORT", "8790"))
COOKIE = "__Host-ro-friend"
OPENKORE_DIR = Path(os.environ.get("OPENKORE_DIR", Path(__file__).resolve().parent.parent))
PANEL = Path(__file__).with_name("kore-panel.html")
ORIGIN = f"https://{PUBLIC_HOST}"

# Which bot the panel looks at: cookie kore-bot (View > Bot). main = logs/ + control/; any other
# instance runs with --control=control-<name> --logs=logs-<name>, its console in ../kore-<name>.log.
BOTS = {"main": ("logs", "control", "kore.log")} | {
    p.name[8:]: (f"logs-{p.name[8:]}", p.name, f"kore-{p.name[8:]}.log")
    for p in OPENKORE_DIR.glob("control-*") if (OPENKORE_DIR / f"logs-{p.name[8:]}").is_dir()}
BOT = contextvars.ContextVar("bot", default="main")


def logs_dir() -> Path:
    return OPENKORE_DIR / BOTS[BOT.get()][0]


def control_dir() -> Path:
    return OPENKORE_DIR / BOTS[BOT.get()][1]


def console_txt() -> Path:
    logs = sorted(logs_dir().glob("console_*_0.txt"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else logs_dir() / "console.txt"


@web.middleware
async def pick_bot(request: web.Request, handler):
    bot = request.cookies.get("kore-bot", "main")
    BOT.set(bot if bot in BOTS else "main")
    return await handler(request)


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
        data = json.loads((logs_dir() / "status.json").read_text())
    except (OSError, ValueError):
        data = {"state": "offline"}
    data |= {"bots": list(BOTS), "bot": BOT.get()}
    return web.json_response(data, headers={"Cache-Control": "no-store"})


# ---- config editor -------------------------------------------------------------------------
# Edits control/config.txt in place. Settings mode changes top-level `key value` lines only; raw
# mode replaces the whole file. Credentials never leave the Mac: their values are masked on read
# and the masked line is restored from disk on write, and the web can't change them.
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
    if not (control_dir() / "backups").exists():
        return []
    return sorted((p.name for p in (control_dir() / "backups").iterdir() if pattern.fullmatch(p.name)), reverse=True)


def write_backed_up(path: Path, stem: str, text: str) -> str:
    """Copy path to backups/<stem>-YYYYmmdd-HHMMSS.txt (keep 20), then atomically replace it."""
    (control_dir() / "backups").mkdir(exist_ok=True)
    name = time.strftime(f"{stem}-%Y%m%d-%H%M%S.txt")
    shutil.copy2(path, (control_dir() / "backups") / name)
    for old in backup_names(stem)[20:]:  # keep the last 20
        ((control_dir() / "backups") / old).unlink()
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_bytes(text.encode("utf-8", "surrogateescape"))
    os.replace(tmp, path)
    return name


def write_config(text: str) -> str:
    return write_backed_up(control_dir() / "config.txt", "config", text)


async def kore_command(cmd: str) -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(str(logs_dir() / "console.socket"))
    except OSError:
        return False
    writer.write(bus_encode("set active"))  # OpenKore ignores input from a client that isn't active
    writer.write(bus_encode("input", {"data": cmd}))
    await writer.drain()
    writer.close()
    return True


def read_config() -> str:
    return (control_dir() / "config.txt").read_bytes().decode("utf-8", "surrogateescape")


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
                new = ((control_dir() / "backups") / body["restore"]).read_bytes().decode("utf-8", "surrogateescape")
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
CONTROL_FILES = ("mon_control.txt items_control.txt pickupitems.txt shop.txt buyer_shop.txt timeouts.txt "
                 "priority.txt routeweights.txt chat_resp.txt responses.txt avoid.txt arrowcraft.txt "
                 "consolecolors.txt").split()
MAX_TEXT = 512 * 1024


def control_file(name: str) -> Path | None:
    """The control file for an allowlisted name, else None (no paths, no config.txt)."""
    if not re.fullmatch(r"[a-z_]+\.txt", name) or name not in CONTROL_FILES:
        return None
    return control_dir() / name


async def files_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    files = [{"name": n, "size": (control_dir() / n).stat().st_size} for n in CONTROL_FILES if (control_dir() / n).is_file()]
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
                new = ((control_dir() / "backups") / body["restore"]).read_bytes().decode("utf-8", "surrogateescape")
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
        reader, writer = await asyncio.open_unix_connection(str(logs_dir() / "console.socket"))
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
    if SPAWNS_DIR.is_dir():  # map ranking over the real server spawns
        r = find_leveling_maps(1, 5)
        assert r and all(1 <= m["level"] <= 5 for x in r for m in x["monsters"]), r[:1]
        assert any(x["map"] == "prt_fild08" for x in find_leveling_maps(1, 10, limit=50))
        acts = []; asyncio.run(run_tool("kore_command", {"label": "x", "command": "conf lockMap a\nquit"}, acts))
        assert acts == [{"label": "x", "command": "conf lockMap a quit"}]
        assert asyncio.run(run_tool("map_monsters", {"map": "nowhere"}, [])).get("error")
    assert "conf keys:" in get_config("buyAuto") or "buyAuto" not in read_config()
    if SPAWNS_DIR.is_dir():
        assert server_npcs(map="izlude_in", item="602")[0]["x"] == 57
    r = ai_finish("x <div><button onclick=\"runCommand('conf saveMap izlude', 'Set')\">Set</button></div>", [], [])
    assert r["reply"] == "x" and r["actions"] == [{"label": "Set", "command": "conf saveMap izlude"}], r
    r = ai_finish("do `ic_set Iron Ore 0 1 0` and `conf lockMap x`", [], [])
    assert [a["command"] for a in r["actions"]] == ["ic_set 1002 0 1 0", "conf lockMap x"], r
    assert prefer_ids("conf useSelf_item_0 Novice Potion, Red Potion") == ("conf useSelf_item_0 569,501", [])
    assert prefer_ids("conf -f buyAuto_0 Butterfly Wing") == ("conf -f buyAuto_0 602", [])
    assert prefer_ids("conf buyAuto_0_npc izlude_in 57 110")[0] == "conf buyAuto_0_npc izlude_in 57 110"
    assert prefer_ids("conf lockMap prt_fild08")[0] == "conf lockMap prt_fild08"
    assert prefer_ids("conf -f attackSkillSlot_0 Envenom")[0] == "conf -f attackSkillSlot_0 52", prefer_ids("conf -f attackSkillSlot_0 Envenom")
    assert prefer_ids("conf useSelf_skill_1 Double Strafe")[0] == "conf useSelf_skill_1 46"
    assert prefer_ids("conf attackSkillSlot_0 TF_POISON")[0] == "conf attackSkillSlot_0 52"
    assert prefer_ids("conf attackSkillSlot_0 인베넘")[0] == "conf attackSkillSlot_0 52"
    assert prefer_ids("conf attackSkillSlot_0_lvl 10")[0] == "conf attackSkillSlot_0_lvl 10"
    assert prefer_ids("conf useSelf_item_1 Nonexistent Thing")[1] == ["Nonexistent Thing"]
    if block_values("useSelf_item")[:1] == ["569"]:
        assert block_clash("conf -f useSelf_item_0 501") and not block_clash("conf useSelf_item_0 569")
        assert not block_clash("conf -f useSelf_item_0_hp < 40")
    assert block_clash("conf -f useSelf_item_7 501", [{"command": "conf -f useSelf_item_7 506"}])
    assert not block_clash("conf -f useSelf_item_7 506", [{"command": "conf -f useSelf_item_7 506"}])
    acts = []
    for c in ("conf lockMap a", "conf lockMap b"):
        asyncio.run(run_tool("kore_command", {"command": c}, acts))
    assert [a["command"] for a in acts] == ["conf lockMap b"], acts
    acts = []
    r = asyncio.run(run_tool("add_config_block", {"block": "useSelf_item", "value": "Green Potion", "options": {"whenStatusActive": "Poison"}}, acts))
    r2 = asyncio.run(run_tool("add_config_block", {"block": "useSelf_item", "value": "Red Potion", "options": {"hp": "< 40"}}, acts))
    assert r2["index"] == r["index"] + 1 and acts[0]["command"].endswith(f"useSelf_item_{r['index']} 506"), acts
    assert prefer_ids("ic_set Iron Ore 0 1 0") == ("ic_set 1002 0 1 0", []), prefer_ids("ic_set Iron Ore 0 1 0")
    assert prefer_ids("ic_set Nope Thing 0 1 0")[1]
    assert hp_percent("conf -f useSelf_item_1_hp < 40") == "conf -f useSelf_item_1_hp < 40%"
    assert hp_percent("conf useSelf_item_1_hp < 400") == "conf useSelf_item_1_hp < 400"
    if '"id": 6008' in (logs_dir() / "status.json").read_text():
        assert prefer_ids("ic_set Wood 0 1 0")[0] == "ic_set 6008 0 1 0", prefer_ids("ic_set Wood 0 1 0")
    for c, ro in (("s", True), ("conf lockMap", True), ("conf lockMap x", False), ("skills", True), ("skills add 5", False),
                  ("storage", True), ("storage get 1", False), ("quest list", True), ("move prontera", False), ("ai off", False),
                  ("comp list", True), ("comp help", True), ("comp summon Darithor", False), ("c @companion heal 80 40", False)):
        assert kore_readonly(c) is ro, c
    assert "roster" in ai_companions() or "error" in ai_companions()
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
    assert control_file("mon_control.txt") == control_dir() / "mon_control.txt"
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
    sd = parse_skill_descriptions("AS_X#\n音速投擲(Sonic)\nMAX Lv : 10\n^777777習得條件 : 拳刃修練 4^000000\n"
                                  "內容 : ^777777快速攻擊\n暈眩^000000\n^ffffff_^000000\n[Lv 1] : ^777777ATK300%^000000\n"
                                  "[Lv10] : ATK750%\n第二行\n#\nTF_Y#\nHiding\nMax Lv : 10\n#\n")
    assert sd["AS_X"] == {"max": 10, "req": "拳刃修練 4", "about": "快速攻擊\n暈眩", "lv": {"1": "ATK300%", "10": "ATK750%\n第二行"}}, sd
    assert sd["TF_Y"]["max"] == 10 and sd["TF_Y"]["lv"] == {}
    # 3x2 bottom-up 8-bit BMP, palette 0 = magenta (transparent), 1 = red
    pal = b"\xff\x00\xff\x00" + b"\x00\x00\xff\x00"
    bmp = b"BM" + struct.pack("<IHHI", 0, 0, 0, 14 + 40 + 8) + struct.pack("<IiiHHIIiiII", 40, 3, 2, 1, 8, 0, 8, 0, 0, 2, 0) \
        + pal + b"\x01\x00\x01\x00" + b"\x00\x01\x00\x00"
    ip = bmp8_png(bmp)
    assert ip[12:16] == b"IHDR" and struct.unpack(">II", ip[16:24]) == (3, 2)
    assert b"PLTE\xff\x00\xff\xff\x00\x00" in ip and b"tRNS\x00\xff" in ip
    rows = zlib.decompress(ip[ip.index(b"IDAT") + 4:ip.index(b"IEND") - 8])
    assert rows == b"\0\x00\x01\x00" + b"\0\x01\x00\x01", rows  # top row first
    print("selftest ok")


# ---- name tables -----------------------------------------------------------------------------
# OpenKore names items/skills from its table folders (rooff-zh, then kRO). The panel can show another
# language: it fetches these id -> name maps and swaps names client side.
LANGS = {"en": "iRO", "zh-tw": "rooff-zh", "zh-cn": "cRO"}
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


# ---- skill window data -----------------------------------------------------------------------
# GET /kore/skillinfo/{lang}.json -> {HANDLE: {max, req, about, lv: {"1": text, ...}}} from the client
# skill descriptions (tables/<server>/skillsdescriptions.txt). GET /kore/skillicon/{handle}.png -> the
# client's skill icon, fetched from Ragnarok Offline's asset server, magenta made transparent.
DESC_DIRS = {"en": "iRO", "zh-tw": "rooff", "zh-cn": "cRO"}
ASSET_URL = os.environ.get("KORE_ASSET_URL", "http://127.0.0.1:3338").rstrip("/")
ICON_DIR = "data/texture/%EC%9C%A0%EC%A0%80%EC%9D%B8%ED%84%B0%ED%8E%98%EC%9D%B4%EC%8A%A4/item/"  # 유저인터페이스
skillinfo_cache: dict[str, bytes] = {}
icon_cache: dict[str, bytes | None] = {}
COLOR_CODE = re.compile(r"\^[0-9a-fA-F]{6}")
REQ_LINE = re.compile(r"(習得條件|学习条件|習得条件|Requirement)\s*[:：]\s*(.+)", re.I)
ABOUT_LINE = re.compile(r"^(內容|内容|Description)\s*[:：]\s*(.*)", re.I)
LV_LINE = re.compile(r"^\[Lv\s*(\d+)\]\s*[:：]?\s*(.*)", re.I)


def parse_skill_descriptions(text: str) -> dict[str, dict]:
    out = {}
    for block in text.split("\n#\n"):
        head, _, body = block.strip("\n").partition("#\n")
        handle = head.strip()
        if not re.fullmatch(r"[A-Z0-9_]+", handle):
            continue
        info = {"max": None, "req": None, "about": "", "lv": {}}
        cur = None  # where continuation lines go: "about" or a level key
        for raw in body.splitlines()[1:]:  # first line is the skill's display name
            line = COLOR_CODE.sub("", raw).strip()
            if not line or line == "_":
                cur = None
                continue
            if m := re.match(r"max\s*lv\s*[:：]\s*(\d+)", line, re.I):
                info["max"] = int(m[1])
            elif m := REQ_LINE.search(line):
                info["req"] = m[2].strip()
            elif m := LV_LINE.match(line):
                cur = m[1].lstrip("0") or "0"
                info["lv"][cur] = m[2].strip()
            elif m := ABOUT_LINE.match(line):
                cur, info["about"] = "about", m[2].strip()
            elif cur == "about":
                info["about"] += "\n" + line
            elif cur:
                info["lv"][cur] += "\n" + line
        out[handle] = info
    return out


async def skillinfo_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    lang = request.match_info["lang"]
    if lang not in DESC_DIRS:
        return web.json_response({"error": "unknown language"}, status=404)
    if lang not in skillinfo_cache:
        path = OPENKORE_DIR / "tables" / DESC_DIRS[lang] / "skillsdescriptions.txt"
        try:
            info = parse_skill_descriptions(path.read_text("utf-8", "replace").replace("\r\n", "\n"))
        except OSError:
            info = {}
        skillinfo_cache[lang] = json.dumps(info, ensure_ascii=False).encode()
    return web.Response(body=skillinfo_cache[lang], content_type="application/json",
                        headers={"Cache-Control": "private, max-age=86400"})


def bmp8_png(bmp: bytes) -> bytes:
    """8-bit palette BMP -> palette PNG; magenta (the client's transparent colour) becomes alpha 0."""
    if bmp[:2] != b"BM" or struct.unpack_from("<H", bmp, 28)[0] != 8 or struct.unpack_from("<I", bmp, 30)[0]:
        raise ValueError("not an uncompressed 8-bit BMP")  # ponytail: the client's skill icons are all 8-bit
    off, dib = struct.unpack_from("<I", bmp, 10)[0], struct.unpack_from("<I", bmp, 14)[0]
    w, h = struct.unpack_from("<ii", bmp, 18)
    ncol = struct.unpack_from("<I", bmp, 46)[0] or 256
    pal = bmp[14 + dib:14 + dib + 4 * ncol]
    plte = b"".join(bytes((pal[i + 2], pal[i + 1], pal[i])) for i in range(0, len(pal), 4))
    trns = bytes(0 if plte[i:i + 3] == b"\xff\x00\xff" else 255 for i in range(0, len(plte), 3))
    stride, rows = (w + 3) & ~3, range(abs(h) - 1, -1, -1) if h > 0 else range(abs(h))
    raw = b"".join(b"\0" + bmp[off + y * stride:off + y * stride + w] for y in rows)
    if len(raw) != abs(h) * (w + 1):
        raise ValueError("truncated BMP")
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, abs(h), 8, 3, 0, 0, 0))
            + png_chunk(b"PLTE", plte) + png_chunk(b"tRNS", trns) + png_chunk(b"IDAT", zlib.compress(raw, 9))
            + png_chunk(b"IEND", b""))


async def skillicon_get(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    handle = request.match_info["handle"].lower()
    if not re.fullmatch(r"[a-z0-9_]{2,40}", handle):
        return web.Response(status=404)
    if handle not in icon_cache:
        png = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(f"{ASSET_URL}/{ICON_DIR}{handle}.bmp") as r:
                    if r.status == 200:
                        png = bmp8_png(await r.read())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, struct.error):
            return web.Response(status=502)  # asset server down: don't cache, retry next time
        icon_cache[handle] = png
    png = icon_cache[handle]
    if png is None:
        return web.Response(status=404)
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "private, max-age=604800"})


# ---- AI advisor ----------------------------------------------------------------------------
# POST /kore/chat {messages:[{role,content}], model?} -> {reply, actions:[{label,command}], tools:[..]}
# Any OpenAI-compatible endpoint (CLIProxyAPI by default, LM Studio works too). The model only
# reads game data through the tools below; it can't run commands. kore_command runs read-only console
# commands; any other command becomes a button in the panel, and the player confirms it before it is sent.
AI_URL = os.environ.get("KORE_AI_URL", "http://127.0.0.1:8317/v1").rstrip("/")
AI_MODEL = os.environ.get("KORE_AI_MODEL", "gemini-3-flash")
AI_KEY_FILE = OPENKORE_DIR.parent / "kore-ai.key"
SPAWNS_DIR = Path(os.environ.get("RATHENA_MOBS", Path.home() / ".ragnarokoffline/rathena-src/npc/re/mobs"))
AI_SYSTEM = """You are a Ragnarok Online (rAthena Renewal, offline server) coach inside an OpenKore bot panel.
Ground every answer in the tools: call get_character first, then find_leveling_maps / map_monsters /
monster_info for maps and get_skills for skill points. Only recommend maps and monsters the tools
return (this server's spawns), and only skills get_skills lists as learnable (up=true).
Consider job, level, stats, weapon element vs monster element/race/size, aggressive monsters and HP.
You can also diagnose and tune the bot like an OpenKore expert: read_log to see what it is doing and why,
get_config / read_control_file for current settings, server_npcs for real NPC coordinates on this server.
Never guess coordinates or settings: look them up. Fixes are kore_command buttons ('conf <key> <value>',
blocks via 'conf <block>_<n>_<key> <value>', e.g. 'conf buyAuto_0_npc izlude_in 57 110'). Key OpenKore facts:
- lockMap <map>: where to level; route there uses portals and Kafra teleports.
- sellAuto 1 + sellAuto_npc <map x y> (+ sellAuto_npc_steps s): sells items_control.txt rows with sell=1.
- itemsMaxWeight_sellOrStore <pct>: weight % that starts a sell/storage trip; too low = constant trips when the
  bag holds unsellable stuff. itemsMaxWeight <pct>: stop picking up loot. Keep sellOrStore BELOW itemsMaxWeight
  (else the bag fills, looting stops and no trip ever starts); ~70-80 is typical. Any shop NPC buys any loot.
- saveMap <map> + saveMap_warpToBuyOrSell 1: Butterfly Wing (or Teleport Lv2) to the save point for sell/buy
  trips; saveMap must equal the real save point (respawn map in the log). Needs Butterfly Wings in the bag:
  pair it with a buyAuto block that restocks them (server_npcs item 'Butterfly Wing' near the save point).
- buyAuto block: the item is the block's value, set with 'conf buyAuto_0 Butterfly Wing'; then keys
  buyAuto_0_npc <map x y>, buyAuto_0_npc_steps b (b = open buy window), buyAuto_0_minAmount, buyAuto_0_maxAmount.
- Save point: respawns land on it, so read_log 'Map Change' right after 'died'/'Respawn'/'save' lines, or ask the
  player. Prefer the town whose shops you use (e.g. Izlude: Tool Dealer izlude_in 57 110, seller izlude 128 158).
- Diagnosing loops: the CAUSE is the 'Auto-storaging due to <reason>' / 'Auto-buying due to insufficient <item>' line
  that starts each trip; count how often it repeats (read_log match 'Auto-'). Warnings inside the trip are effects.
- 'storage: <item> out of stock' comes from getAuto (fetch from storage), never buyAuto; buyAuto only buys at the
  NPC and never checks storage. It is harmless: OpenKore stops asking for that item once storage opened.
- Weight-trip loop: 'due to itemsMaxWeight' every visit = weight stays >= itemsMaxWeight_sellOrStore after storing.
  Check get_character weight and items_control.txt rows with store=0 (format: item keep store sell) on heavy stacks,
  and 'Unable to store' / 'cannot be stored' items (add '<id> 0 0 0' so it stops trying).
- Buy loop: buyAuto minAmount must be well below maxAmount; minAmount >= the amount held after one trip (a wing used
  to return) rebuys every visit.
- Stat/skill points: 'stat_add <str|agi|vit|int|dex|luk>' adds ONE point per command (no amount). For many points
  or a build plan use the raiseStat plugin (loaded): 'conf statsAddAuto_list 30 agi, 30 dex, 50 agi' (cumulative
  targets, applied in order, spends points as they arrive) + 'conf statsAddAuto 1'. Values >99 need statsAdd_over_99 1.
  Skills: raiseSkill plugin: 'conf skillsAddAuto_list NV_BASIC 9, TF_DOUBLE 10' (skill HANDLES + target level, in
  order) + 'conf skillsAddAuto 1'. Prefer these lists over many single commands when the player wants a build.
- Job change: this server may run the Job Master mod (instant job change); look it up first with server_npcs
  name 'custom/jobmaster', then the official quest NPCs (name 'jobs/<job>').
'conf' only changes keys that already exist in config.txt (get_config shows them); to add a missing key, often a
block option like useSelf_item_0_hp, use 'conf -f <key> <value>'.
Items in config (useSelf_item, buyAuto, getAuto...): write the exact English item name (as get_character/server_npcs
show it, e.g. 'Green Potion'); the gateway converts it to the item ID the bot needs. Never type an ID from memory.
RULES: only use conf keys that get_config shows or that are listed here; never invent keys or step codes.
If a fact (save point, coordinates) isn't in tool output, say so instead of guessing.
- storageAuto 1 + storageAuto_npc <map x y> (storageAuto_npc_type 1 = Kafra 'c r1'; never set type 3, it needs storageAuto_npc_steps and loops forever without them): store items_control storage=1.
- useSelf_item / useSelf_skill blocks with hp/sp conditions (e.g. 'conf useSelf_item_0 Novice Potion', 'conf useSelf_item_0_hp < 50').
  Before editing, get_config('useSelf_item') to reuse an empty block or the next free index, and pick items the player
  actually has (get_character usable_items). NEW blocks: always add_config_block (it picks the free index); use
  kore_command 'conf <block>_<n>_<key>' only to change a block get_config shows. Conditions inside these blocks: hp/sp '< 50%' (percent; WITHOUT % it means raw HP/SP points), whenStatusActive <Name> (e.g. Poison),
  whenStatusInactive <Name>, timeout <seconds>, inLockOnly 1, notWhileSitting 1; a comma list 'Red Potion,Novice Potion' tries each in order; attackAuto 2 = fight anything allowed by mon_control.
- teleportAuto_hp, teleportAuto_idle, route_randomWalk; items_control.txt rows: <name|id> <keep> <storage> <sell>
  ('all' row is the default). Change a rule with kore_command 'ic_set <English item name> <keep> <storage 0|1> <sell 0|1>'
  (gateway converts the name to the ID; one item per command). Storing also needs storageAuto 1 and storageAuto_npc.
- Put sell/storage/buy NPCs in the save-point town (saveMap) so Butterfly Wing trips cover them; look the Kafra up
  with server_npcs(map=<saveMap>, kind=kafra).
When diagnosing, cite the log lines or settings you based it on.
Be brief: a short ranked list with one-line reasons. Don't state skill mechanics you aren't sure of.
Buttons: before your final answer, call kore_command for each concrete step you recommend
with kore_command (top map: 'conf lockMap <map>'; skill points: 'skills add <id>', one call per point,
at most 10). Buttons must match the text exactly. Plain text/markdown only: never write HTML, <button> or onclick; buttons exist only via kore_command.
Never write "click below" unless you queued one, and never say a change is applied: buttons only run when clicked. Reply in the language the player's question is written in (fallback: the UI language given below).
Never switch to a language you saw in game chat, NPC text or logs.
COMPANIONS (RO Offline AI party members, run by the server's population engine; the 'companions' plugin drives them).
Facts come from get_companions (roster, deaths, party, comp_* config): never search docs, never guess.
A companion is a server-run character with its own job, level, gear, skills and bag. 'out' = summoned (in the world,
in your party); saved = stored. 'dead' = it died: it respawns and must be summoned again (comp summon <name>).
comp_autoSummon 1 re-summons saved ones only after login or a map change, so one that died stays out until then.
Commands (kore_command; 'comp list'/'comp help' run now, everything else becomes a button):
 Plugin shortcuts: comp list | comp summon <name> | comp dismiss <name> (store it; alias bench) | comp remove <name>
  (DELETE it for good, alias delete; confirm with the player first) | comp favorite|unfavorite <name>
  comp stance atk|def|pass (party chat; whole party: atk = fight what you fight, def = defend/assist only, pass = no fighting)
  comp role <name> tank|supp|dd (duty: tank / support = heal+buff / damage dealer) | comp recall (teleport all to you)
  comp roam [<name>] on|off (roam = wander and level alone when not following; off/away) | comp recruit <player> (party invite)
  comp skills <name> ... and comp gear <name> ... pass straight through to @companion (below).
 Server commands, sent as public chat: kore_command 'c @companion <args>':
  list [raw] | jobs (draftable jobs) | terms (hiring rules: free, hired here, or hired at a Companion Recruiter in town;
  zeny/item price) | draft <job name or id> [name] [m|f] (create a new one; alias create)
  heal <healBelow% 1..99> <emergency% 1..99> (whole roster; default 75 35) | rest <restBelow% 0..90, 0=never>
  <restUntil% up to 100, >= restBelow+5> (sit to regen; default 30 95)
  skills <name> (list) | skills <name> toggle <skill id> | skills <name> only <ids/names> | skills <name> all|none|auto
  gear <name> [weapon shield armor shoes garment acc head costume shadow ammo] (alias unequip: takes its worn gear back
   into YOUR bag, all slots or just those named; overweight drops at your feet)
  spare <name> list|raw (gear it carries but doesn't wear) | spare <name> stow <slot> | spare <name> wear <#> | spare <name> take <#>
  homunculus <name> [on|off] (alias pet; its homunculus) | rebirth <name> novice|trans (rebirth to High Novice / advance
   to transcendent; needs the right level, always confirm) | roam [<name>] on|off
 Prefer the plugin shortcuts above. Raw party chat equivalents (kore_command 'p <text>'): '<name> tank|support|attacker'
  (duty, one companion), 'attack' / 'defensive' / 'passive' (stance, ALWAYS whole party, never with a name), 'recall'.
 Plugin config: comp_autoSummon 1, comp_stanceTown pass, comp_stanceField atk (stance sent on entering town/field),
  comp_partyName <name> (party created when not in one).
Survival: a companion far below the map's monster levels dies again and again (sleep/stun stops its healing). Compare
its lv with map_monsters of the lockMap; suggest a map in range for BOTH (find_leveling_maps around its level..+10),
stance def, a higher heal/rest threshold, or dismissing it while you farm hard maps. Count deaths in get_companions first."""

AI_TOOLS = [
    {"name": "get_character", "description": "The bot character, live: HP/SP now and max, position, AI mode, current action, buffs/ailments, target, job, levels, exp, stats, free stat/skill points, HP/SP, map, equipment, usable_items and other_items in the bag (name + id), exp per hour, current lockMap.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_companions", "description": "RO Offline companions: roster (name, job, lv, out/saved, dead, duty, heal/rest "
     "thresholds, roam), party members, the plugin's comp_* config, and recent companion events (deaths, joins, leaves) with times.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_skills", "description": "Every skill in the character's tree: id, handle, name, level, up (true = a point can be added now).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "find_leveling_maps", "description": "Rank this server's maps by how many spawned monsters fall in a level range. Returns the best maps with those monsters.",
     "parameters": {"type": "object", "properties": {
         "min_level": {"type": "integer"}, "max_level": {"type": "integer"},
         "kind": {"type": "string", "enum": ["any", "field", "dungeon"], "description": "default any"},
         "avoid_aggressive": {"type": "boolean", "description": "skip maps where most in-range monsters are aggressive"}},
         "required": ["min_level", "max_level"]}},
    {"name": "map_monsters", "description": "All monsters that spawn on one map (e.g. prt_fild07), with level, HP, race, element, size, aggressive, count; zh = the Chinese name shown in the log/game.",
     "parameters": {"type": "object", "properties": {"map": {"type": "string"}}, "required": ["map"]}},
    {"name": "monster_info", "description": "Stats of a monster by English or Chinese (zh) name or id, and the maps where it spawns.",
     "parameters": {"type": "object", "properties": {"monster": {"type": "string"}}, "required": ["monster"]}},
    {"name": "read_log", "description": "Recent OpenKore console log (kore.log), newest last. Use to diagnose: why it sells, "
     "where it walks, stuck routes, NPC talk loops, errors. Optional regex filter (case-insensitive).",
     "parameters": {"type": "object", "properties": {"match": {"type": "string"}, "lines": {"type": "integer", "description": "default 40, max 150"}}}},
    {"name": "get_config", "description": "Lines of control/config.txt whose key matches a regex (e.g. 'sellAuto|itemsMaxWeight', 'buyAuto'). "
     "Blocks (buyAuto {...}, attackSkillSlot {...}) come whole, labelled with the conf key prefix to change them (buyAuto_0_npc).",
     "parameters": {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]}},
    {"name": "read_control_file", "description": "Lines of a control file matching a regex: items_control.txt (<item> <keep> <storage> <sell>), "
     "mon_control.txt, pickupitems.txt, timeouts.txt, routeweights.txt, avoid.txt, shop.txt.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "match": {"type": "string"}}, "required": ["name"]}},
    {"name": "server_npcs", "description": "This server's NPCs from its scripts: shops (with item names), warps, Kafras, and every other script NPC "
     "(kind 'npc': job changers, quest and custom NPCs, with their script file, e.g. name 'assassin' or 'jobs/' finds job "
     "quests, 'custom/' = stock custom NPCs like Job Master/Warper/Healer/Stylist, 'mods/' = app mod NPCs). Filter by map, name (NPC name or script path) and/or "
     "item (name or id, e.g. 'Butterfly Wing' finds who sells it). Coordinates are what OpenKore's *_npc settings need.",
     "parameters": {"type": "object", "properties": {"map": {"type": "string"}, "item": {"type": "string"},
                                                     "kind": {"type": "string", "enum": ["any", "shop", "warp", "kafra", "npc"]},
                                                     "name": {"type": "string"}}}},
    {"name": "add_config_block", "description": "Add a NEW config block (useSelf_item, useSelf_skill, attackSkillSlot, buyAuto, "
     "getAuto, partySkill, ...). The gateway picks the next free index and turns item names into IDs, then queues buttons. "
     "Use this instead of guessing block indexes.",
     "parameters": {"type": "object", "properties": {
         "block": {"type": "string", "description": "e.g. useSelf_item"},
         "value": {"type": "string", "description": "the block's value: item name(s) for item blocks, skill name for skill blocks"},
         "options": {"type": "object", "description": "block keys, e.g. {\"hp\": \"< 40\"} or {\"whenStatusActive\": \"Poison\"}"},
         "label": {"type": "string"}},
         "required": ["block", "value"]}},
    {"name": "kore_command", "description": "Run any OpenKore console command (same as typing it in the console). Read-only "
     "commands run now and return their output: s, st, i [eq|u|nu], skills, skills desc <id>, ml, pl, nl, il, exp, where, cart, "
     "storage, party, quest list, conf <key>, help <command>. Every other command (conf <key> <value>, skills add <id>, "
     "stat_add <stat>, move <map>, ai ..., sell, ...) is NOT run: it becomes a button the player confirms. Useful: "
     "'conf lockMap <map>' (bot levels there), 'skills add <skill id>' (one point), 'stat_add <str|agi|vit|int|dex|luk>' (one point).",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"},
         "label": {"type": "string", "description": "short button text, used when the command needs confirmation"}},
         "required": ["command"]}},
]
_ai_data: dict = {}


def ai_data() -> dict:
    """Monster table, spawns and English names, loaded once."""
    if _ai_data:
        return _ai_data
    mobs = {}
    rows = (OPENKORE_DIR / "tables" / "monsters_table.txt").read_text("utf-8", "replace").splitlines()
    head = rows[0].split("\t")
    for row in rows[1:]:
        r = dict(zip(head, row.split("\t")))
        if r.get("ID", "").isdigit():
            mobs[int(r["ID"])] = {"id": int(r["ID"]), "name": r["Name"], "level": int(r["Level"] or 0), "hp": int(r["Hp"] or 0),
                                  "race": r["Race"], "element": f'{r["Element"]} {r["ElementLevel"]}', "size": r["Size"],
                                  "aggressive": r.get("isAIMode_Aggressive") == "1", "mvp": r.get("isAIMode_MVP") == "1"}
    zh = OPENKORE_DIR / "tables" / "rooff-zh" / "monsters.txt"  # Chinese names the log/user use
    for line in (zh.read_text("utf-8-sig", "replace").splitlines() if zh.is_file() else []):
        i, _, n = line.strip().partition(" ")
        if i.isdigit() and int(i) in mobs and n:
            mobs[int(i)]["zh"] = n.strip()
    spawns: dict[str, dict] = {}  # map -> {"kind", "mobs": {id: count}}
    for f in sorted(SPAWNS_DIR.rglob("*.txt")):
        kind = "dungeon" if "dungeon" in f.parts else "field"
        for line in f.read_text("utf-8", "replace").splitlines():
            p = line.split("\t")
            if len(p) < 4 or line.lstrip().startswith("//") or p[1] not in ("monster", "boss_monster"):
                continue
            m = p[0].split(",")[0]
            idc = p[3].split(",")
            if not (idc[0].isdigit() and len(idc) > 1 and idc[1].isdigit()):
                continue
            e = spawns.setdefault(m, {"kind": kind, "mobs": {}})
            e["mobs"][int(idc[0])] = e["mobs"].get(int(idc[0]), 0) + int(idc[1])
    maps = {}
    for line in (OPENKORE_DIR / "tables" / "iRO" / "maps.txt").read_text("utf-8", "replace").splitlines():
        p = line.split("#")
        if len(p) >= 2 and p[0].endswith(".rsw"):
            maps[p[0][:-4]] = p[1]
    _ai_data.update(mobs=mobs, spawns=spawns, maps=maps,
                    skills=name_table(OPENKORE_DIR / "tables" / "iRO" / "skillnametable.txt"),
                    items=name_table(OPENKORE_DIR / "tables" / "iRO" / "items.txt"),
                    tw=name_table(OPENKORE_DIR / "tables" / "twRO" / "items.txt"))
    return _ai_data


def _mob(mid: int, count: int | None = None) -> dict:
    m = dict(ai_data()["mobs"].get(mid) or {"id": mid, "name": f"#{mid}"})
    if count is not None:
        m["count"] = count
    return m


def find_leveling_maps(min_level: int, max_level: int, kind: str = "any", avoid_aggressive: bool = False, limit: int = 8) -> list:
    d, out = ai_data(), []
    for m, e in d["spawns"].items():
        if kind != "any" and e["kind"] != kind:
            continue
        total = sum(e["mobs"].values())
        hit = [_mob(i, c) for i, c in e["mobs"].items() if min_level <= d["mobs"].get(i, {}).get("level", -1) <= max_level]
        n = sum(x["count"] for x in hit)
        if not n or any(x.get("mvp") for x in hit):
            continue
        aggro = sum(x["count"] for x in hit if x.get("aggressive"))
        if avoid_aggressive and aggro * 2 > n:
            continue
        out.append({"map": m, "name": d["maps"].get(m, ""), "kind": e["kind"], "in_range": n, "total_spawn": total,
                    "share": round(n / total, 2), "monsters": sorted(hit, key=lambda x: -x["count"])[:6]})
    out.sort(key=lambda r: (-(r["in_range"] * r["share"]), r["map"]))
    return out[:limit]


def ai_companions() -> dict:
    try:
        s = json.loads((logs_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return {"error": "bot offline"}
    c = s.get("companions") or {}
    names = [m.get("name") for m in c.get("members", []) if m.get("name")]
    events = []
    if names:
        rx = re.compile("|".join(map(re.escape, names)))
        ev = re.compile(r"死亡|died|Died|離隊|left|加入|joined|summon|召喚")
        try:
            lines = (console_txt()).read_text("utf-8", "replace").splitlines()[-20000:]
        except OSError:
            lines = []
        events = [l[:160] for l in lines if rx.search(l) and ev.search(l) and "@CP|" not in l][-25:]
    return {"roster": c.get("members", []),
            "roster_age_s": int(time.time() - c["updated"]) if c.get("updated") else None,
            "party": s.get("party"), "map": s.get("map"), "you_lv": s.get("lv"),
            "config": {k: v for _, k, v in config_lines(read_config()) if k.startswith("comp_")},
            "events": events}


def ai_character() -> dict:
    try:
        s = json.loads((logs_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return {"error": "bot offline"}
    items = ai_data()["items"]
    lock = next((v for _, k, v in config_lines(read_config()) if k == "lockMap"), "")
    return {k: s.get(k) for k in ("name", "job", "lv", "job_lv", "hp", "hp_max", "sp", "sp_max", "map", "x", "y", "ai", "action",
                                  "state", "zeny", "exp", "exp_max", "job_exp", "job_exp_max", "base_exp_hour", "job_exp_hour",
                                  "zeny_hour", "weight", "weight_max", "statuses", "target")} | {
        "stats": s.get("stats"), "lockMap": lock,
        "equipment": [{"slot": i["equipped"], "name": items.get(str(i["id"]), i["name"])} for i in s.get("inventory", []) if i.get("equipped")],
        "usable_items": [{"name": items.get(str(i["id"]), i["name"]), "id": i["id"], "amount": i["amount"]} for i in s.get("inventory", []) if i.get("usable")],
        "other_items": [{"name": items.get(str(i["id"])) or ai_data()["tw"].get(str(i["id"]), i["name"]), "id": i["id"], "amount": i["amount"]}
                        for i in s.get("inventory", []) if not i.get("usable") and not i.get("equipped")]}


def ai_skills() -> list:
    try:
        s = json.loads((logs_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return [{"error": "bot offline"}]
    names = ai_data()["skills"]
    return [{"id": k["id"], "handle": k["handle"], "name": names.get(k["handle"], k["handle"]), "lv": k["lv"], "up": k["up"], "target": k["target"]}
            for k in s.get("skills", [])]


ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_NOISE = re.compile(r"^(Player |\[dist=|Monster .* is casting Emotion|Processing map )")


def read_log(match: str = "", lines: int = 40) -> str:
    path = OPENKORE_DIR.parent / BOTS[BOT.get()][2]
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 400_000))
        text = ANSI.sub("", f.read().decode("utf-8", "replace"))
    rows = [r for r in text.splitlines()[1:] if r.strip() and not LOG_NOISE.match(r)]
    if match:
        rx = re.compile(match, re.I)
        rows = [r for r in rows if rx.search(r)]
    return "\n".join(rows[-max(1, min(int(lines or 40), 150)):])


def get_config(match: str) -> str:
    rx, out, counts, block = re.compile(match, re.I), [], {}, None
    for line in mask(read_config()).split("\n"):
        m = re.match(r"^\s*([A-Za-z_]\w*)\b(.*?)\{\s*$", line)
        if m and block is None:
            n = counts[m.group(1)] = counts.get(m.group(1), -1) + 1
            block = [f"# conf keys: {m.group(1)}_{n}_<key>", line] if rx.search(m.group(1)) else []
            continue
        if block is not None:
            if block:
                block.append(line)
            if line.strip() == "}":
                out += block; block = None
            continue
        key = line.split(None, 1)[0] if line.strip() else ""
        if key and not key.startswith("#") and rx.search(key):
            out.append(line)
    return "\n".join(out[:200]) or "(no matching keys)"


def read_control_file(name: str, match: str = "") -> str:
    path = control_file(name)
    if path is None or not path.is_file():
        return f"not allowed or missing; files: {CONTROL_FILES}"
    rx = re.compile(match, re.I) if match else None
    rows = [r for r in path.read_text("utf-8", "replace").splitlines() if r.strip() and (rx is None or rx.search(r))]
    return "\n".join(rows[:120]) + (f"\n... {len(rows) - 120} more" if len(rows) > 120 else "")


MOD_MAP_CONF = Path.home() / "Library/Application Support/Ragnarok Offline/state/conf/map_conf.txt"


def server_scripts() -> list[Path]:
    """Script files the Renewal server loads, in load order."""
    root, seen, out = SPAWNS_DIR.parents[2], set(), []
    def walk(conf: Path):
        if conf in seen or not conf.exists():
            return
        seen.add(conf)
        for line in conf.read_text("utf-8", "replace").splitlines():
            m = re.match(r"\s*(import|npc):\s*(\S+)", line)
            if m:
                (walk if m.group(1) == "import" else out.append)(root / m.group(2))
    walk(root / "npc/re/scripts_main.conf")
    # Mod NPCs (job master, warper, healer, adventurer guide...): the app's generated map_conf lists them;
    # npc/mods/<x> lives in state/modbuild/npc/<x>, stock npc/custom/* in rathena-src.
    if MOD_MAP_CONF.exists():
        for m in re.finditer(r"^npc:\s*(\S+)", MOD_MAP_CONF.read_text("utf-8", "replace"), re.M):
            rel = m.group(1)
            out.append(MOD_MAP_CONF.parents[1] / "modbuild" / "npc" / rel[len("npc/mods/"):] if rel.startswith("npc/mods/") else root / rel)
    return out


def npc_index() -> list[dict]:
    stamp = MOD_MAP_CONF.stat().st_mtime if MOD_MAP_CONF.exists() else 0  # app rewrites it when mods change
    if "npcs" in _ai_data and _ai_data.get("npcs_stamp") == stamp:
        return _ai_data["npcs"]
    _ai_data["npcs_stamp"] = stamp
    items, byname = ai_data()["items"], {}
    head = re.compile(r"^([a-z0-9_@-]+),(\d+),(\d+),\d+\t(shop|cashshop|warp2?|script|duplicate\([^)]*\))\t([^\t]+)\t(.*)$")
    for f in server_scripts():
        if not f.exists():
            continue
        for line in f.read_text("utf-8", "replace").splitlines():
            m = head.match(line)
            if not m:
                continue
            mp, x, y, kind, name, rest = m.groups()
            e = {"map": mp, "x": int(x), "y": int(y), "name": name.split("::")[0]}
            if kind in ("shop", "cashshop"):
                ids = re.findall(r",(\d+):-?\d+", rest)
                e |= {"kind": "shop", "items": [f"{items.get(i, i)} ({i})" for i in ids], "ids": ids}
            elif kind.startswith("warp"):
                w = rest.split(",")
                e |= {"kind": "warp", "to": f"{w[2]} {w[3]} {w[4]}" if len(w) >= 5 else rest}
            elif "kafra" in name.lower():
                e |= {"kind": "kafra"}
            elif int(x) > 1:  # other script NPCs (job changers, quests, custom); x<=1 = hidden helpers
                e |= {"kind": "npc", "script": ("mods/" if "/modbuild/" in str(f) else "") + str(f).split("/npc/")[-1]}
            else:
                continue
            byname[(name, mp)] = e  # later (Renewal) files override
    _ai_data["npcs"] = list(byname.values())
    return _ai_data["npcs"]


def server_npcs(map: str = "", item: str = "", kind: str = "any", name: str = "") -> list:
    item, name = item.strip().lower(), name.strip().lower()
    out = []
    for e in npc_index():
        if map and e["map"] != map.lower():
            continue
        if kind not in ("", "any") and e["kind"] != kind:
            continue
        if name and name not in (e["name"] + " " + e.get("script", "")).lower():
            continue
        if item and not (e["kind"] == "shop" and (item in e["ids"] or any(item in str(n).lower() for n in e["items"]))):
            continue
        out.append({k: v for k, v in e.items() if k != "ids"})
    if name:  # NPCs whose script file matches (job quest files, mods) before mere name matches
        out.sort(key=lambda e: name not in e.get("script", "").lower())
    return out[:40] or [{"error": "nothing found"}]


# conf keys whose value is an item list; the bot's own names are Korean, so these must be IDs.
ITEM_KEY = re.compile(r"^(useSelf_item|buyAuto|getAuto|sellAuto_item|storageAuto_item|equipAuto_\d+_\w+|attackEquip_\w+|"
                      r"partySkill_\d+_target_item|cartAuto_item)(_\d+)?$", re.I)


def item_ids(value: str) -> tuple[str, list[str]]:
    """'Red Potion, 569' -> ('501,569', unknown names). Matches English (iRO), Chinese and Korean tables."""
    d = ai_data()
    if "item_lookup" not in d:
        look = {}
        for lang in ("kRO", "rooff-zh", "twRO", "iRO"):
            d[lang] = name_table(OPENKORE_DIR / "tables" / lang / "items.txt")
            for i, n in d[lang].items():
                look.setdefault(n.lower(), i)
        d["item_lookup"] = look
    # Items the character carries win: one name can match several IDs (Wood: 6688, and 6008 which has
    # no English name but the same Chinese one). Expand a match through every language's name for it.
    owned = set()
    try:
        owned = {str(it["id"]) for it in json.loads((logs_dir() / "status.json").read_text()).get("inventory", [])}
    except (OSError, ValueError):
        pass
    if "names_to_ids" not in d:
        rev = {}
        for lang in ("kRO", "rooff-zh", "twRO", "iRO"):
            for i, n in d[lang].items():
                rev.setdefault(n.lower(), set()).add(i)
        d["names_to_ids"] = rev
    def resolve(key):
        ids = set(d["names_to_ids"].get(key, ()))
        for i in list(ids):   # same item in other languages -> other IDs sharing those names
            for lang in ("kRO", "rooff-zh", "twRO", "iRO"):
                if (n := d[lang].get(i)):
                    ids |= d["names_to_ids"].get(n.lower(), set())
        mine = sorted(ids & owned)
        return mine[0] if mine else d["item_lookup"].get(key)
    out, unknown = [], []
    for part in [p.strip() for p in value.split(",") if p.strip()]:
        key = part.lower().replace("_", " ")
        if part.isdigit():
            out.append(part)
        elif (i := resolve(key)):
            out.append(i)
        else:
            out.append(part); unknown.append(part)
    return ",".join(out), unknown


def block_values(name: str) -> list[str]:
    """Values of each '<name> <value> {' block in config.txt, in order (index = position)."""
    return [m.group(1).strip() for m in re.finditer(rf"^{re.escape(name)}\b(.*?)\{{\s*$", read_config(), re.M)]


CONF_SET = re.compile(r"^conf\s+(?:-f\s+)?(\S+)\s+(.+)$")


def block_clash(cmd: str, pending: list | None = None) -> str | None:
    """Error text if 'conf <block>_<n> <value>' would replace a block that already holds something else,
    in config.txt or in a button queued earlier in this answer."""
    m = re.match(r"^conf\s+(?:-f\s+)?([A-Za-z_]+?)_(\d+)\s+(.+)$", cmd)
    if not m:
        return None
    vals = block_values(m.group(1))
    for a in pending or []:
        q = re.match(rf"^conf\s+(?:-f\s+)?{re.escape(m.group(1))}_(\d+)\s+(.+)$", a["command"])
        if q:
            i = int(q.group(1))
            vals += [""] * (i + 1 - len(vals))
            vals[i] = q.group(2).strip()
    n = int(m.group(2))
    if n < len(vals) and vals[n] and vals[n] != m.group(3).strip():
        free = next((i for i, v in enumerate(vals) if not v), len(vals))
        return f"{m.group(1)}_{n} already holds '{vals[n]}'; use {m.group(1)}_{free}"
    return None


def hp_percent(cmd: str) -> str:
    """'conf [-f] x_hp < 50' -> '< 50%': OpenKore reads a bare number as HP points, which nobody means for <= 100."""
    m = re.match(r"^(conf\s+(?:-f\s+)?\S+_(?:hp|sp))\s+([<>]=?)\s*(\d+)\s*$", cmd)
    return f"{m.group(1)} {m.group(2)} {m.group(3)}%" if m and int(m.group(3)) <= 100 else cmd


# conf keys whose value is a skill; the bot's skill names are Korean (kRO), so use the skill ID.
SKILL_KEY = re.compile(r"^(attackSkillSlot|useSelf_skill|partySkill|attackComboSlot)_\d+(_afterSkill)?$", re.I)


def skill_id(value: str) -> str | None:
    """'Envenom' / '인베넘' / 'TF_POISON' / '52' -> '52'; None when no table knows it."""
    d = ai_data()
    if "skill_lookup" not in d:
        ids = {}
        for line in (OPENKORE_DIR / "tables" / "SKILL_id_handle.txt").read_text("utf-8", "replace").splitlines():
            if (p := line.split()) and len(p) >= 2 and p[0].isdigit():
                ids[p[1]] = p[0]
        look = {i: i for i in ids.values()}
        for lang in ("kRO", "rooff-zh", "twRO", "iRO"):
            for h, n in name_table(OPENKORE_DIR / "tables" / lang / "skillnametable.txt").items():
                for alias in [n] + re.split(r"[()]", n):   # 'Double Strafe(Double strafing)' -> both
                    if alias.strip() and h in ids:
                        look.setdefault(alias.strip().lower(), ids[h])
        look.update({h.lower(): i for h, i in ids.items()})
        d["skill_lookup"] = look
    return d["skill_lookup"].get(value.strip().lower().replace("_", " ")) or d["skill_lookup"].get(value.strip().lower())


def prefer_ids(cmd: str) -> tuple[str, list[str]]:
    """Rewrite 'conf [-f] <item key> <names>' and 'ic_set <name> ...' to item IDs (and hp/sp to percent)."""
    cmd = hp_percent(cmd)
    ic = re.match(r"^ic_set\s+(.+?)\s+(\d+)\s+([01])\s+([01])\s*$", cmd)
    if ic:
        ids, unknown = item_ids(ic.group(1))
        return (f"ic_set {ids} {ic.group(2)} {ic.group(3)} {ic.group(4)}", unknown) if "," not in ids else (cmd, [ic.group(1)])
    m = re.match(r"^(conf\s+(?:-f\s+)?)(\S+)\s+(.+)$", cmd)
    if m and SKILL_KEY.match(m.group(2)):
        h = skill_id(m.group(3))
        return (f"{m.group(1)}{m.group(2)} {h}", []) if h else (cmd, [m.group(3).strip()])
    if not m or not ITEM_KEY.match(m.group(2)):
        return cmd, []
    ids, unknown = item_ids(m.group(3))
    return f"{m.group(1)}{m.group(2)} {ids}", unknown


KORE_READ = {"s", "st", "i", "skills", "ml", "pl", "nl", "il", "exp", "where", "cart", "storage", "party", "quest", "help", "conf",
             "eq", "stats", "pml", "friend", "guild", "pet", "homun", "merc", "weight", "status", "comp"}


def kore_readonly(cmd: str) -> bool:
    """True for console commands that only print. Sub-commands that change things (skills add,
    conf key value, cart add, storage get, ...) are not."""
    w = cmd.split()
    head, rest = w[0].lower(), [x.lower() for x in w[1:]]
    if head not in KORE_READ:
        return False
    if head == "conf":
        return len(rest) == 1
    if head in ("skills",):
        return not rest or rest[0] == "desc"
    if head == "quest":
        return rest[:1] == ["list"]
    if head == "comp":
        return rest[:1] in (["list"], ["help"])
    if head in ("cart", "storage", "guild", "pet", "homun", "merc", "friend", "party", "exp"):
        return not rest or rest[0] in ("info", "list", "desc", "s", "status", "eq", "u", "nu", "log", "report", "monster", "item")
    return True


async def run_tool(name: str, args: dict, actions: list) -> object:
    d = ai_data()
    if name == "get_character":
        return ai_character()
    if name == "get_skills":
        return ai_skills()
    if name == "get_companions":
        return ai_companions()
    if name == "find_leveling_maps":
        return find_leveling_maps(int(args.get("min_level", 1)), int(args.get("max_level", 999)), args.get("kind", "any"), bool(args.get("avoid_aggressive")))
    if name == "map_monsters":
        e = d["spawns"].get(str(args.get("map", "")).lower())
        return {"map": args.get("map"), "name": d["maps"].get(args.get("map"), ""), "kind": e["kind"],
                "monsters": sorted((_mob(i, c) for i, c in e["mobs"].items()), key=lambda x: x.get("level", 0))} if e else {"error": "no spawns on that map"}
    if name == "monster_info":
        q = str(args.get("monster", "")).strip().lower()
        hit = [m for m in d["mobs"].values() if q in (str(m["id"]), m["name"].lower(), m.get("zh", ""))] or \
              [m for m in d["mobs"].values() if q and (q in m["name"].lower() or q in m.get("zh", ""))][:5]
        return [m | {"maps": sorted(k for k, e in d["spawns"].items() if m["id"] in e["mobs"])[:15]} for m in hit[:5]] or {"error": "unknown monster"}
    if name == "read_log":
        return read_log(str(args.get("match", "")), int(args.get("lines") or 40))
    if name == "get_config":
        return get_config(str(args.get("match", ".")))
    if name == "read_control_file":
        return read_control_file(str(args.get("name", "")), str(args.get("match", "")))
    if name == "server_npcs":
        return server_npcs(str(args.get("map", "")), str(args.get("item", "")), str(args.get("kind", "any")), str(args.get("name", "")))
    if name == "add_config_block":
        block = str(args.get("block", "")).strip()
        if not re.fullmatch(r"[A-Za-z_]+", block):
            return {"error": "bad block name"}
        vals = block_values(block)
        for a in actions:
            q = re.match(rf"^conf\s+(?:-f\s+)?{re.escape(block)}_(\d+)\s+(.+)$", a["command"])
            if q:
                i = int(q.group(1)); vals += [""] * (i + 1 - len(vals)); vals[i] = q.group(2)
        n = next((i for i, v in enumerate(vals) if not v), len(vals))
        cmd, unknown = prefer_ids(f"conf -f {block}_{n} {args.get('value', '')}")
        if unknown:
            return {"error": f"unknown item/skill name(s) {unknown}: use the English name as get_character/server_npcs show it"}
        cmds = [cmd] + [hp_percent(f"conf -f {block}_{n}_{k} {v}") for k, v in (args.get("options") or {}).items()
                        if re.fullmatch(r"[A-Za-z_]+", str(k))]
        label = str(args.get("label") or f"Add {block}_{n}")[:60]
        for i, c in enumerate(cmds):
            actions.append({"label": label if i == 0 else f"{label}: {c.split()[2].split('_')[-1]}"[:60], "command": re.sub(r"[\r\n]+", " ", c)[:200]})
        return {"queued": cmds, "index": n}
    if name == "kore_command":
        cmd = re.sub(r"[\r\n]+", " ", str(args.get("command", ""))).strip()[:200]
        if not cmd:
            return {"error": "empty command"}
        # Repeats of the same one-point command (stat_add vit x5) become one button running them
        # all, chained with OpenKore's ';;' separator.
        if re.match(r"(stat_add|st\s+add|skills\s+add)\b", cmd, re.I):
            same = next((a for a in actions if a.get("base") == cmd), None)
            if same:
                same["n"] += 1
                same["command"] = ";;".join([cmd] * same["n"])
                same["label"] = f'{same["label"].split(" ×")[0]} ×{same["n"]}'
                return {"queued": True, "command": cmd, "note": f"merged into one button, now x{same['n']}"}
            actions.append({"label": str(args.get("label") or cmd)[:60], "command": cmd, "base": cmd, "n": 1})
            return {"queued": True, "command": cmd, "note": "shown to the player as a button; not run yet"}
        if kore_readonly(cmd):
            try:
                return {"output": (await kore_capture(cmd, 1.5))[-6000:]}
            except OSError:
                return {"error": "bot offline"}
        cmd, unknown = prefer_ids(cmd)
        if clash := block_clash(cmd, actions):
            return {"error": clash + ". Re-send all of that block's keys with the new index; to replace, tell the player to do it in Advanced config."}
        if unknown:
            return {"error": f"unknown item/skill name(s) {unknown}: use the item or skill ID (get_character usable_items, server_npcs ids)"}
        key = (CONF_SET.match(cmd) or [None, None])[1]
        actions[:] = [a for a in actions if not key or (CONF_SET.match(a["command"]) or [None, None])[1] != key]
        actions.append({"label": str(args.get("label") or cmd)[:60], "command": cmd})
        return {"queued": True, "command": cmd, "note": "shown to the player as a button; not run yet"}
    return {"error": f"unknown tool {name}"}


RUNCMD = re.compile(r"""runCommand\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]*)['"])?""")


def ai_finish(reply: str, actions: list, used: list) -> dict:
    """If the model wrote HTML buttons anyway, turn them into real ones and drop the markup."""
    for cmd, label in RUNCMD.findall(reply):
        cmd = prefer_ids(cmd)[0]
        if not any(a["command"] == cmd for a in actions):
            actions.append({"label": (label or cmd)[:60], "command": cmd[:200]})
    reply = re.sub(r"<(div|button)\b.*?</\1>", "", reply, flags=re.S).strip()
    # Commands it only typed in backticks become buttons too (same ID conversion and slot checks).
    for cmd in re.findall(r"`((?:conf|ic_set)\s[^`\n]+)`", reply):
        cmd, unknown = prefer_ids(cmd.strip())
        if not unknown and not block_clash(cmd, actions) and not any(a["command"] == cmd for a in actions):
            actions.append({"label": cmd[:60], "command": cmd[:200]})
    return {"reply": reply, "actions": actions, "tools": used}


async def ai_ask(messages: list, model: str | None = None, lang: str = "en", convo: list | None = None) -> dict:
    """`convo` (optional) receives the full exchange, tool calls and results included."""
    import aiohttp
    key = AI_KEY_FILE.read_text().strip() if AI_KEY_FILE.exists() else ""
    convo = [] if convo is None else convo
    convo += [{"role": "system", "content": AI_SYSTEM + f"\nUI language: {lang}."}] + messages
    tools = [{"type": "function", "function": t} for t in AI_TOOLS]
    actions, used = [], []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as http:
        rounds = 16  # ponytail: fixed cap; the last round has no tools, so it must answer
        for n in range(rounds):
            last = n == rounds - 1
            if last:
                convo.append({"role": "user", "content": "Stop looking things up. Answer now with what you found."})
            async with http.post(f"{AI_URL}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                                 json={"model": model or AI_MODEL, "messages": convo} | ({} if last else {"tools": tools})) as r:
                body = await r.json(content_type=None)
            if "choices" not in body:
                return {"error": str(body.get("error", body))[:300]}
            msg = body["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return ai_finish(msg.get("content") or "", actions, used)
            convo.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    result = await run_tool(fn["name"], args, actions)
                except Exception as e:  # bad arguments from the model: tell it, don't crash
                    result = {"error": str(e)}
                used.append(fn["name"])
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)[:12000]})
    return {"reply": "(no answer after the last round)", "actions": actions, "tools": used}


AI_SESSIONS = logs_dir() / "ai-sessions"  # ponytail: never pruned; delete the folder when it grows


async def models_get(request: web.Request) -> web.Response:
    """Chat models the AI endpoint offers (OpenAI /models), default first."""
    if not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    import aiohttp
    key = AI_KEY_FILE.read_text().strip() if AI_KEY_FILE.exists() else ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.get(f"{AI_URL}/models", headers={"Authorization": f"Bearer {key}"}) as r:
                ids = [m["id"] for m in (await r.json(content_type=None)).get("data", [])]
    except Exception as e:
        return web.json_response({"default": AI_MODEL, "models": [AI_MODEL], "error": str(e)[:200]})
    # ponytail: name heuristic drops non-chat models; add an env allowlist if it guesses wrong
    ids = sorted({i for i in ids if not re.search(r"tts|voice|image|video|veo|embed|whisper|audio|review", i)})
    return web.json_response({"default": AI_MODEL, "models": [AI_MODEL] + [i for i in ids if i != AI_MODEL]})


async def chat_post(request: web.Request) -> web.Response:
    if request.headers.get("Origin") != ORIGIN or not authed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
        msgs = [{"role": m["role"], "content": str(m["content"])[:4000]} for m in body["messages"][-20:] if m.get("role") in ("user", "assistant")]
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad request"}, status=400)
    try:
        model = body.get("model") if re.fullmatch(r"[\w.:/\-]{1,128}", str(body.get("model") or "")) else None
        lang = str(body.get("lang") or "en") if re.fullmatch(r"[A-Za-z-]{2,16}", str(body.get("lang") or "")) else "en"
        sid = str(body.get("session") or "")
        convo: list = []
        try:
            res = await ai_ask(msgs, model, lang, convo)
        finally:
            # Each turn rewrites logs/ai-sessions/<sid>.json, so a pasted session ID shows Claude Code
            # exactly what the advisor saw (tool results included).
            if re.fullmatch(r"[a-z0-9]{6,32}", sid):
                AI_SESSIONS.mkdir(parents=True, exist_ok=True)
                (AI_SESSIONS / f"{sid}.json").write_text(json.dumps({"model": model or AI_MODEL, "convo": convo}, ensure_ascii=False, indent=1))
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"error": f"AI unreachable: {e}"}, status=502)


app = web.Application(middlewares=[pick_bot])
app.router.add_post("/_friend/exchange", exchange)
app.router.add_get("/ws/{target}", socket)
app.router.add_get("/", panel)
app.router.add_get("/kore/status", status)
app.router.add_get("/kore/console", console)
app.router.add_get("/kore/config", config_get)
app.router.add_post("/kore/config", config_post)
app.router.add_get("/kore/files", files_get)
app.router.add_get("/kore/names/{lang}.json", names_get)
app.router.add_get("/kore/skillinfo/{lang}.json", skillinfo_get)
app.router.add_get("/kore/skillicon/{handle}.png", skillicon_get)
app.router.add_get("/kore/file/{name}", file_get)
app.router.add_post("/kore/file/{name}", file_post)
app.router.add_get("/kore/map/{field}.png", map_get)
app.router.add_post("/kore/chat", chat_post)
app.router.add_get("/kore/models", models_get)

async def kore_capture(cmd: str, wait: float = 2.0) -> str:
    """Run one console command and return what OpenKore prints within `wait` s (ongoing bot
    chatter included)."""
    reader, writer = await asyncio.open_unix_connection(str(logs_dir() / "console.socket"))
    writer.write(bus_encode("set active"))
    writer.write(bus_encode("input", {"data": cmd}))
    await writer.drain()
    buf, out = bytearray(), []
    loop = asyncio.get_running_loop()
    end = loop.time() + wait
    while (left := end - loop.time()) > 0:
        try:
            data = await asyncio.wait_for(reader.read(65536), left)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf += data
        while (decoded := bus_decode(buf)) is not None:
            mid, args = decoded
            if mid == "output":
                out.append(str(args.get("message", "")))
    writer.close()
    return "".join(out)


async def cli_command(cmd: str, wait: float = 2.0) -> None:
    """Run one console command in the background and print what OpenKore answers within `wait` s."""
    print(await kore_capture(cmd, wait), end="", flush=True)


if __name__ == "__main__" and sys.argv[1:] == ["selftest"]:
    selftest()
elif __name__ == "__main__" and sys.argv[1:2] == ["ask"] and len(sys.argv) == 3:
    print(json.dumps(asyncio.run(ai_ask([{"role": "user", "content": sys.argv[2]}])), ensure_ascii=False, indent=1))
elif __name__ == "__main__" and sys.argv[1:2] == ["cmd"] and len(sys.argv) == 3:
    asyncio.run(cli_command(sys.argv[2]))
elif __name__ == "__main__":
    print(f"invitation: https://{PUBLIC_HOST}/#invite={INVITE}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
