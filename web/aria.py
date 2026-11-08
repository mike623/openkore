# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""Aria: an OpenKore character with an LLM brain, played as the owner's party companion.

OpenKore (control-aria/, logs-aria/) does the fighting. This script listens to its console
socket and wakes the LLM only on events: party chat or whispers, her death, a level-up.
The LLM answers in party chat and runs OpenKore commands, including changing her own config.

Guardrails: connection/credential keys and dangerous commands are refused; every config change
is backed up and logged (logs-aria/aria-changes.log); changes she makes on her own are capped
at 10 an hour and rolled back if she dies twice within 10 minutes of one. Only the owner's
orders run commands; other people get a chat reply.

usage: uv run aria.py [selftest]   (env: ARIA_OWNER, ARIA_NAME, ARIA_CONTROL, ARIA_LOGS)
start Kore first: perl openkore.pl --interface=Socket --control=control-aria --logs=logs-aria
"""

import asyncio
import importlib.util
import json
import re
import shutil
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("gw", HERE / "kore-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)  # ponytail: reuse the gateway's bus codec and LLM settings

import os
OWNER = os.environ.get("ARIA_OWNER", "coucoucou")
NAME = os.environ.get("ARIA_NAME", "Aria")
MODEL = os.environ.get("ARIA_MODEL", "gpt-6-luna")
CONTROL = gw.OPENKORE_DIR / os.environ.get("ARIA_CONTROL", "control-aria")
LOGS = gw.OPENKORE_DIR / os.environ.get("ARIA_LOGS", "logs-aria")
SOCK = LOGS / "console.socket"
NOTES = LOGS / "aria-notes.md"
CHANGES = LOGS / "aria-changes.log"
BACKUPS = LOGS / "conf-backups"

PERSONA = f"""You are {NAME}, a player character in Ragnarok Online (rAthena renewal, private offline server),
partied with {OWNER}, your friend. You are cheerful, a bit cautious, and talk like a real player in party chat:
short lines, casual, a text smiley now and then (:) ^^ :D xD, never emoji: the game can't show them). Language: always English; only if the latest message is written in
Chinese characters, answer in Traditional Chinese. Never any other language. Never say you are an AI or a bot.

OpenKore runs your body: it fights, follows {OWNER}, uses potions by its config. You are its mind. You act with tools:
- party(text): say something in party chat. Always answer {OWNER} this way (or whisper if they whispered).
- kore(command): run an OpenKore console command, e.g. `sp 28 {OWNER}` (cast skill 28 on them), `follow {OWNER}`,
  `follow stop`, `move <map> [x y]`, `sit`, `stand`, `stat_add vit`, `skills add <id>`, `conf <key> <value>`,
  `conf <key>` (read), `i` (inventory), `skills`, `st` (stats), `autosell`, `autostorage`, `autobuy`, `tele`.
  Config changes persist. Use them to tune yourself: attackDistance, useSelf_skill_N_*, useSelf_item_N_*,
  attackAuto, lockMap, teleportAuto_hp, buyAuto_N_*, partySkill_N_* ...
- status(): your HP/SP, level, job, stats, skills, inventory, map.
- remember(note): save a fact for later (likes, plans, lessons). Your notes are below.

Rules: do what {OWNER} asks if it is possible; if not, say why in one line. Don't argue, don't lecture.
NEVER say you did, changed or set something unless a kore() call in this turn did it and its output confirms it.
When {OWNER} asks you to play differently, translate it into config: read the current value with `conf <key>`, set the
new one with `conf <key> <value>`, then say the exact change. Useful keys: attackAuto (0 never fight, 1 only monsters
attacking you or the party, 2 fight anything), attackAuto_party, follow (1/0), followTarget, lockMap, route_randomWalk,
attackDistance, followDistanceMax, useSelf_item_N_hp, teleportAuto_hp.
{OWNER} writes casual, non-native English with typos: read for intent, not keywords. "you self auto attack first" /
"you fight alone first" means: level up on your own (attackAuto 2, follow 0, maybe lockMap a safe field). If a message
can mean two different things, ask one short question instead of guessing.
Answer only the LATEST message. Earlier lines are context; don't repeat old answers.
Look things up instead of guessing: find_leveling_maps (where to level for your level), map_monsters, monster_info,
server_npcs (job change NPCs, shops, Kafras with real coordinates on this server; search shops by item
without a map: they are mostly indoors). An empty lookup means try a wider search, not "it doesn't exist".
Game facts: a Novice needs job level 10 to change to a first job, and needs to fight to get there. If a request
contradicts the game rules, say so in one line and propose what works.
While follow is 1, never move to another map (shopping, quests) unless {OWNER} said to go: ask first, e.g.
"Hat shop is in Prontera, go now?".
Gear: `eq <#idx>` wears an inventory item (idx from status() inventory), `uneq <#idx>` removes it, `i eq` / `i neq`
list worn / wearable items. There is no 'equip' command. "You can't put on" = wrong job or level for that item.
Check status() before claiming facts about yourself. Keep chat lines under 80 characters, one or two lines.
When nobody asked anything (death, level-up), decide yourself: tune config, spend points, or say a short line.
"""

ROLE = CONTROL / "aria-role.md"  # her job guide; edit freely, read on every message
LOOKUPS = {"find_leveling_maps", "map_monsters", "monster_info", "server_npcs"}
# The gateway advisor's OpenKore/server know-how, minus its button and panel parts.
KNOWLEDGE = gw.AI_SYSTEM[gw.AI_SYSTEM.index("Key OpenKore facts"):gw.AI_SYSTEM.index("When diagnosing")]
PERSONA += ("\nOpenKore and server knowledge (written for a coach panel: where it says kore_command or buttons, use your "
            "kore tool instead; your commands run at once):\n" + KNOWLEDGE)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOISE = re.compile(r"^(\[dist=|NPC Exists|Portal Exists|Player |Monster |\(From: |\(To |\[Party\] |Achievement)")
PARTY = re.compile(r"^\[Party\] (.+?) : (.*)$")
WHISPER = re.compile(r"^\(From: (.+?)\) : (.*)$")
DEATH = re.compile(r"^You have died")
MOVE = re.compile(r"^move\s+([a-z][\w@-]*)(?:\s+(\d+)\s+(\d+))?\s*$", re.I)
CONF_SET = re.compile(r"^conf\s+(?:-f\s+)?(\S+)\s+\S")
DENY_HEAD = {"eval", "quit", "relog", "plugin", "reload", "dump", "charselect", "debug", "rc", "exec", "auth", "bus"}
DENY_KEY = re.compile(r"^(master|server|username|password|char|loginPinCode|otpSeed|XKore.*|adminPassword|"
                      r"secureAdminPassword|bindIp|forceMapIP|poseidon.*|followTarget)$", re.I)


def kore_commands() -> str:
    """Every console command OpenKore registers (src/Commands.pm), name + one line; syntax via `help <cmd>`."""
    # ponytail: core commands only; plugin commands (comp, ...) come from the persona/knowledge text.
    t = lambda x: re.sub(r'T\(\s*"((?:[^"\\]|\\.)*)"\s*\)', r'"\1"', x)
    q = r'"((?:[^"\\]|\\.)*)"'
    try:
        src = (gw.OPENKORE_DIR / "src/Commands.pm").read_text("utf-8", "replace")
        body = src[src.index("sub initHandlers"):]
        body = body[:body.index("\n\t);")]
    except (OSError, ValueError):
        return ""
    out = []
    for m in re.finditer(r"^\t\t\['([\w-]+)',(.*?)(?=^\t\t\['|\Z)", body, re.S | re.M):
        name, chunk = m.group(1), t(m.group(2))
        if name in DENY_HEAD:
            continue
        desc = re.search(q, chunk)
        out.append(f"{name}: {desc.group(1) if desc else ''}")
    return "\n".join(out)


PERSONA += ("\nOpenKore console commands (the ONLY valid kore() commands; never invent a name). Before using one "
            "you haven't used, run kore('help <command>') for its exact syntax. '#' = list index from status() or the "
            "list command:\n" + kore_commands())


PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                       "\u2026": "...", "\u00a0": " ", "\u200d": ""})


def game_text(text: str) -> str:
    """Chat the client can show: the server speaks Big5, which mangles curly quotes and has no emoji."""
    def ok(c: str) -> bool:
        try:
            return ord(c) < 128 or bool(c.encode("big5"))
        except UnicodeEncodeError:
            return False
    out = "".join(c for c in text.translate(PUNCT) if ok(c))
    return re.sub(r"\s{2,}", " ", out).strip()


def guard(cmd: str, owner_asked: bool) -> str | None:
    """Why `cmd` is refused, or None. followTarget only when the owner asked."""
    words = cmd.split()
    if not words:
        return "empty command"
    if words[0].lower() in DENY_HEAD:
        return f"'{words[0]}' is not allowed"
    m = re.match(r"^conf\s+(?:-f\s+)?(\S+)", cmd, re.I)  # reads too: `conf password` would print it
    if m and DENY_KEY.match(m.group(1)) and not (owner_asked and m.group(1).lower() == "followtarget"):
        return f"config key '{m.group(1)}' is not allowed"
    return None


def parse(line: str) -> dict | None:
    line = ANSI.sub("", line).strip()
    if m := PARTY.match(line):
        return None if m.group(1) == NAME else {"kind": "party", "from": m.group(1), "text": m.group(2)}
    if m := WHISPER.match(line):
        return {"kind": "whisper", "from": m.group(1), "text": m.group(2)}
    if DEATH.match(line):
        return {"kind": "death"}
    return None


def status() -> dict:
    try:
        d = json.loads((LOGS / "status.json").read_text())
    except (OSError, ValueError):
        return {"state": "offline"}
    keep = ("state", "name", "lv", "job", "job_lv", "hp", "hp_max", "sp", "sp_max", "map", "x", "y", "zeny",
            "weight", "weight_max", "stats", "statuses", "action", "target", "party")
    out = {k: d.get(k) for k in keep}
    out["deal"] = d.get("deal")
    out["owner"] = next(({k: p.get(k) for k in ("x", "y", "dist")} for p in d.get("players") or [] if p.get("name") == OWNER),
                        "not on screen")
    out["skills"] = [{"id": s["id"], "handle": s["handle"], "lv": s["lv"]} for s in d.get("skills") or []]
    out["inventory"] = [f'#{i.get("idx")} {i["name"]} x{i["amount"]}' + (" (worn)" if i.get("equipped") else "")
                        for i in (d.get("inventory") or [])][:40]
    return out


async def kore(cmd: str, wait: float = 1.5) -> str:
    reader, writer = await asyncio.open_unix_connection(str(SOCK))
    writer.write(gw.bus_encode("set active"))
    await writer.drain()
    try:  # a new connection is first sent recent console history; skip it so it isn't read as our answer
        while await asyncio.wait_for(reader.read(65536), 0.4):
            pass
    except asyncio.TimeoutError:
        pass
    writer.write(gw.bus_encode("input", {"data": cmd}))
    await writer.drain()
    buf, out, loop = bytearray(), [], asyncio.get_running_loop()
    end = loop.time() + wait
    while (left := end - loop.time()) > 0:
        try:
            data = await asyncio.wait_for(reader.read(65536), left)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf += data
        while (msg := gw.bus_decode(buf)) is not None:
            if msg[0] == "output":
                out.append(ANSI.sub("", str(msg[1].get("message", ""))))
    writer.close()
    # Drop what nearby players and NPCs said meanwhile; it reads like an answer to the command.
    lines = [l for l in "".join(out).splitlines() if l.strip() and not NOISE.match(l)]
    return "\n".join(lines)[-3000:]


class Brain:
    def __init__(self):
        self.auto_changes: list[float] = []   # times of config changes she made on her own
        self.last_backup: Path | None = None
        self.last_auto: float = 0
        self.deaths: list[float] = []
        self.history: list[dict] = []         # recent chat turns, for context
        self.goal: dict | None = None         # last `move` target; reaching it wakes her up

    def log_change(self, cmd: str, why: str, auto: bool) -> None:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        self.last_backup = BACKUPS / f"config-{int(time.time())}.txt"
        shutil.copy2(CONTROL / "config.txt", self.last_backup)
        with CHANGES.open("a") as f:
            f.write(f"{time.strftime('%F %T')} {'auto' if auto else 'asked'} {cmd} | {why}\n")
        if auto:
            self.auto_changes.append(time.time())
            self.last_auto = time.time()

    async def run_tool(self, name: str, args: dict, ev: dict) -> object:
        owner = ev.get("from") == OWNER
        if name == "party":
            text = game_text(str(args.get("text", "")).replace("\n", " "))[:160]
            if ev["kind"] == "whisper":
                return await kore(f'pm "{ev["from"]}" {text}', 0.3)
            return await kore(f"p {text}", 0.3) or "said"
        if name == "status":
            return status()
        if name in LOOKUPS:
            return await gw.run_tool(name, args, [])
        if name == "remember":
            with NOTES.open("a") as f:
                f.write(f"- {time.strftime('%F')} {str(args.get('note', ''))[:300]}\n")
            return "saved"
        if name == "kore":
            if ev.get("from") and not owner:
                return {"error": f"only {OWNER} can give you orders; just chat with this person"}
            cmd = re.sub(r"[\r\n]+", " ", str(args.get("command", ""))).strip()[:200]
            if why := guard(cmd, owner):
                return {"error": why}
            cmd, unknown = gw.prefer_ids(cmd)  # item/skill names in config -> IDs, as the gateway does
            if unknown:
                return {"error": f"unknown item/skill name(s) {unknown}: use the English name server_npcs/status show"}
            if CONF_SET.match(cmd):
                auto = not owner
                if auto and len([t for t in self.auto_changes if t > time.time() - 3600]) >= 10:
                    return {"error": "you already changed 10 settings this hour; wait"}
                self.log_change(cmd, str(args.get("why", ev.get("text", ev["kind"])))[:120], auto)
            if m := MOVE.match(cmd):  # wake again on arrival to finish the errand
                self.goal = {"map": m.group(1), "x": m.group(2), "y": m.group(3), "t": time.time()}
            try:
                return await kore(cmd) or "(no output)"
            except OSError:
                return {"error": "OpenKore is not running"}
        return {"error": f"unknown tool {name}"}

    async def rollback_if_needed(self) -> None:
        now = time.time()
        self.deaths = [t for t in self.deaths if t > now - 600] + [now]
        if self.last_backup and self.last_auto > now - 600 and len([t for t in self.deaths if t > self.last_auto]) >= 2:
            shutil.copy2(self.last_backup, CONTROL / "config.txt")
            await kore("reload config")
            with CHANGES.open("a") as f:
                f.write(f"{time.strftime('%F %T')} rollback to {self.last_backup.name} (2 deaths after own change)\n")
            self.last_auto = 0
            await kore("p undid my last settings change, it wasn't working 😓", 0.3)

    async def think(self, ev: dict) -> None:
        if ev["kind"] == "death":
            await self.rollback_if_needed()
            prompt = "You just died. Check status(), decide what to tune so it happens less, and say a short line."
        elif ev["kind"] == "arrived":
            prompt = (f"You arrived where you walked to ({ev['where']}). Finish the errand you went there for now "
                      f"(see the earlier messages: talk to the NPC, buy/sell, ...), then tell {OWNER} and turn follow back on.")
        elif ev["kind"] == "levelup":
            prompt = f"You reached base level {ev['lv']}. Spend new stat/skill points sensibly for your job, maybe say a line."
        else:
            prompt = f"{ev['from']} ({ev['kind']}): {ev['text']}"
        self.history = (self.history + [{"role": "user", "content": f"LATEST MESSAGE, answer this one: {prompt}"}])[-8:]
        # Older turns lose the marker so only one message reads as "answer this".
        for h in self.history[:-1]:
            if h["role"] == "user":
                h["content"] = h["content"].replace("LATEST MESSAGE, answer this one: ", "(earlier) ")
        notes = NOTES.read_text()[-3000:] if NOTES.exists() else "(none yet)"
        role = ROLE.read_text().replace("{OWNER}", OWNER) if ROLE.exists() else ""
        convo = [{"role": "system", "content": PERSONA + f"\n{role}\nYour notes:\n{notes}\nNow: {json.dumps(status(), ensure_ascii=False)[:2500]}"}]
        convo += self.history
        tools = [{"type": "function", "function": f} for f in TOOLS]
        key = gw.AI_KEY_FILE.read_text().strip() if gw.AI_KEY_FILE.exists() else ""
        spoke, did = False, []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as http:
            for _ in range(16):  # ponytail: fixed round cap; NPC dialogs need many steps
                async with http.post(f"{gw.AI_URL}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                                     json={"model": MODEL, "messages": convo, "tools": tools}) as r:
                    body = await r.json(content_type=None)
                if "choices" not in body:
                    print("LLM error:", str(body)[:300], flush=True)
                    return
                msg = body["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    text = (msg.get("content") or "").strip()
                    if text and not spoke and ev["kind"] in ("party", "whisper"):
                        await self.run_tool("party", {"text": text}, ev)
                    # Remember what was actually run, so a later "did you set it?" is answered from fact.
                    self.history.append({"role": "assistant", "content": (text or "") + (f"\n[ran: {'; '.join(did)}]" if did else "\n[ran no commands]")})
                    return
                convo.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for c in calls:
                    fn = c["function"]
                    try:
                        result = await self.run_tool(fn["name"], json.loads(fn.get("arguments") or "{}"), ev)
                    except Exception as e:
                        result = {"error": str(e)}
                    spoke |= fn["name"] == "party"
                    if fn["name"] == "kore":
                        did.append(str(json.loads(fn.get("arguments") or "{}").get("command", "")))
                    print(f"[{ev['kind']}] {fn['name']} {fn.get('arguments')} -> {str(result)[:120]}", flush=True)
                    convo.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)[:6000]})


TOOLS = [
    {"name": "party", "description": "Say one short line in party chat (or whisper back if they whispered).",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "kore", "description": "Run one OpenKore console command and get its output.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"},
                                                     "why": {"type": "string", "description": "for config changes: why"}},
                    "required": ["command"]}},
    {"name": "status", "description": "Your current HP/SP, level, job, stats, skills, inventory, map.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "remember", "description": "Save a short note for the future.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
]

TOOLS += [t for t in gw.AI_TOOLS if t["name"] in LOOKUPS]


async def main() -> None:
    brain, queue = Brain(), asyncio.Queue()

    async def listen():
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(SOCK))
                writer.write(gw.bus_encode("set active"))
                await writer.drain()
                print("connected to", SOCK, flush=True)
                buf = bytearray()
                while data := await reader.read(65536):
                    buf += data
                    while (msg := gw.bus_decode(buf)) is not None:
                        if msg[0] == "output":
                            for line in str(msg[1].get("message", "")).splitlines():
                                if (ev := parse(line)) and (ev["kind"] != "party" or ev["from"] == OWNER
                                                            or NAME.lower() in ev["text"].lower()):
                                    await queue.put(ev)
            except OSError:
                pass
            await asyncio.sleep(5)  # Kore restarting

    async def watch_level():
        lv = status().get("lv")
        while True:
            await asyncio.sleep(10)
            now = status().get("lv")
            if lv and now and now > lv:
                await queue.put({"kind": "levelup", "lv": now})
            lv = now or lv
            if g := brain.goal:
                st = status()
                if time.time() - g["t"] > 1200:  # ponytail: 20 min cap, a failed route never "arrives"
                    brain.goal = None
                elif st.get("map") == g["map"] and (not g["x"] or max(abs(int(g["x"]) - (st.get("x") or 0)),
                                                                     abs(int(g["y"]) - (st.get("y") or 0))) <= 4):
                    brain.goal = None
                    await queue.put({"kind": "arrived", "where": f'{g["map"]} {g["x"] or ""} {g["y"] or ""}'.strip()})

    async def work():
        while True:
            ev = await queue.get()
            try:
                await brain.think(ev)
            except Exception as e:
                print("think failed:", e, flush=True)

    await asyncio.gather(listen(), watch_level(), work())


def selftest() -> None:
    assert parse(f"\x1b[0;32m[Party] {OWNER} : aria buff me\x1b[0m") == {"kind": "party", "from": OWNER, "text": "aria buff me"}
    assert parse(f"[Party] {NAME} : hi") is None
    assert parse("(From: Bob) : hey") == {"kind": "whisper", "from": "Bob", "text": "hey"}
    assert parse("You have died") == {"kind": "death"}
    assert MOVE.match("move prt_in 172 132").groups() == ("prt_in", "172", "132") and MOVE.match("move izlude")
    assert not MOVE.match("move stop 1 2 3")
    assert guard("conf attackDistance 5", False) is None
    assert guard("conf password x", True) and guard("conf -f XKore 2", True) and guard("eval 1", True)
    assert guard("conf followTarget Bob", False) and guard("conf followTarget Bob", True) is None
    assert guard("conf password", True) and guard("conf attackDistance", False) is None
    assert NOISE.match("[dist=2.8] Faecos (0): hi") and NOISE.match("(From: x) : y") and not NOISE.match("Config 'attackAuto' is 2")
    assert {t["name"] for t in TOOLS} >= LOOKUPS and "Key OpenKore facts" in PERSONA
    assert game_text("Okay, I\u2019ll go \u2728 now\u2026 \U0001F3C3\u200d") == "Okay, I'll go now..."
    assert game_text("好的，我來補血 ^^") == "好的，我來補血 ^^"
    print("selftest ok")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["selftest"] else asyncio.run(main())
