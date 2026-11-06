# Kore web panel — contracts

A web replacement for OpenKore's Wx interface, served at https://kore.namike.me by `kore-gateway.py`
(aiohttp, uv script) and used mostly from iPhone Safari (390px wide). Three parts, each owned by one
file:

| Part | File | Owner |
|---|---|---|
| Bot state export | `plugins/webstatus/webstatus.pl` | plugin |
| HTTP API | `web/kore-gateway.py` | gateway |
| UI | `web/kore-panel.html` (vanilla JS/CSS, single file, no build) | panel |

OpenKore runs with `--interface=Socket`; the gateway relays its console (`logs/console.socket`, Bus
protocol) to the browser as a WebSocket. Never restart the running OpenKore or gateway processes;
the integrator does.

## Auth (existing, unchanged)
Invite exchange sets cookie `__Host-ro-friend`. Every `/kore/*` route needs the cookie; every POST and
the WebSocket also need `Origin: https://kore.namike.me`. 403 otherwise.

## Existing routes (unchanged)
- `GET /` — the panel.
- `GET /kore/status` — `logs/status.json` (below), or `{"state":"offline"}`.
- `GET /kore/console` (WS) — server→browser `{"ev":"output","type","message","domain"}`,
  `{"ev":"title","title"}`, `{"ev":"inputted","data"}`, `{"ev":"error","message"}`;
  browser→server `{"input":"<console command>"}`.
- `GET /kore/config` → `{"settings":[{"key","value","locked"}],"raw","backups":[names]}`;
  `POST /kore/config` with `{"changes":{k:v}}` | `{"raw":text}` | `{"restore":backupName}`
  → `{"ok","backup","reloaded"}` or `{"error"}` (400). Credentials are masked as `********`.

## New routes (gateway)
- `GET /kore/files` → `{"files":[{"name":"mon_control.txt","size":1234}]}` — allowlist:
  `mon_control.txt items_control.txt pickupitems.txt shop.txt buyer_shop.txt timeouts.txt
  priority.txt routeweights.txt chat_resp.txt responses.txt avoid.txt arrowcraft.txt consolecolors.txt`
  (only those that exist in `control/`). `config.txt` stays on `/kore/config`.
- `GET /kore/file/{name}` → `{"name","text","backups":[names newest first]}`.
- `POST /kore/file/{name}` with `{"text"}` | `{"restore":backupName}` → backup to
  `control/backups/<stem>-YYYYmmdd-HHMMSS.txt` (keep 20 per file), atomic write, then console
  command `reload <stem>` (e.g. `reload mon_control`) → `{"ok","backup","reloaded"}` | `{"error"}`.
- `GET /kore/map/{field}.png` — walkability image of `fields/<field>.fld2(.gz)`, 1 px per
  cell, **row 0 = top = highest y** (RO y grows north). Walkable = light, blocked = dark, water =
  blue. Cache in memory. 404 if no such field. `field` must match `[A-Za-z0-9@_\-.]+`.
  Response header `X-Map-Size: <w>x<h>`.

- `GET /kore/names/{lang}.json` (`en` = tables/iRO, `zh-tw` = twRO, `zh-cn` = cRO) →
  `{"items":{id:name},"skills":{handle:name}}`; missing names filled from the other two tables.
  The panel's header picker (EN / 繁中 / 简中 / Bot) swaps item and skill names client side.

## status.json (plugin, written every ~1 s, atomic)
Existing keys (keep): `time state name lv job_lv job hp hp_max sp sp_max exp exp_max job_exp
job_exp_max zeny weight weight_max map x y ai action target monsters[{name,x,y,dist}]
base_exp_hour job_exp_hour zeny_hour`.

New keys (null / [] when unknown):
- `field` — field file basename for `/kore/map/{field}.png` (e.g. `izlude`; differs from `map` on
  instanced maps).
- `stats` — `{str,agi,vit,int,dex,luk, str_bonus,…luk_bonus, points_free, skill_points_free,
  atk,atk_bonus,matk_min,matk_max,def,def_bonus,mdef,mdef_bonus,hit,flee,flee_bonus,critical,aspd,
  sex}`
- `statuses` — `["Blessing", …]`
- `inventory` — `[{"idx","id","name","amount","type","equipped","identified","broken","usable"}]`
  `idx` is what console commands take (`eq <idx>`, `uneq <idx>`, `is <idx>`, `drop <idx> [n]`).
  `type`: `usable|etc|equip|card|arrow|other`. `equipped`: slot name string or null.
- `cart` — null or `{"items":[same shape],"count","count_max","weight","weight_max"}`.
- `storage` — null or `{"open":bool,"items":[same shape],"count","count_max"}` (last known list).
- `skills` — `[{"handle","name","lv","max_lv","sp","target":"self|actor|location|passive"}]`.
- `players` — `[{"name","x","y","dist","job","lv","guild","party","idx"}]` nearest 30.
- `npcs` — `[{"name","x","y","dist","idx"}]` nearest 30 (`idx` is what `talk <idx>` takes).
- `portals` — `[{"name","x","y","dist","dest"}]`.
- `items_ground` — `[{"name","x","y","dist","amount","idx"}]` (`take <idx>`).
- `monsters` gains `"idx"` (what `a <idx>` takes) and `"hp_pct"` when known.
- `party` — null or `{"name","members":[{"name","map","x","y","hp","hp_max","online","leader"}]}`.
- `guild` — null or `{"name","lv","members_online"}`.
- `homun`, `merc`, `pet` — null or `{"name","lv","hp","hp_max","sp","sp_max","hunger","intimacy",
  "exp","exp_max"}` (fields that don't apply = null).
- `talk` — null when no NPC conversation, else `{"npc","messages":[last ≤10 lines],"state":
  "next"|"responses"|"number"|"text"|"close","responses":["…"]}`.
  Console answers: `talk cont`, `talk resp <n>`, `talk num <n>`, `talk text <s>`, `talk no`.
- `deal` — null or `{"with","state","you":[{name,amount}],"them":[{name,amount}],"zeny_you","zeny_them"}`.
- `exp_report` — `{"seconds","base","job","zeny","monsters":[{"name","count"}],"items":[{"name","count"}]}`
  from OpenKore's own counters (`%monsters_Killed`, `%itemChange`, `$totalBaseExp`…).

## UI (panel) — Wx parity on a phone
Bottom tab bar: **Status**, **Map**, **Items**, **Skills**, **Chat**, **Config**, **More**.
- Status: existing card + stats block (+ buttons to add stat points: `stat_add str` …), statuses,
  homun/merc/pet cards, party list, exp report (`exp`, `exp reset` buttons).
- Map: canvas with `/kore/map/{field}.png`, pinch/drag zoom, dots for you / monsters / players /
  NPCs / portals / ground items, tap map → `move <x> <y>`, tap actor → small menu (attack `a <idx>`,
  talk `talk <idx>`, take `take <idx>`, look/pm player).
- Items: Inventory / Cart / Storage sub-tabs; tap item → actions (use `is`, equip `eq`, unequip
  `uneq`, drop, to cart `cart add`, from cart `cart get`, storage `storage add/get`, sell queue).
- Skills: list with level, SP, use (`ss <handle> [lv]` self, `sm <handle> <monster idx>`, `sp`
  player, `sl` location) and level up (`skills add <handle>`).
- Chat: tabs All/Public/Party/Guild/PM built by filtering console `domain`
  (`publicchat`, `pm`, `partychat`, `guildchat`, `selfchat`, `schat`); send box with mode
  (`c`, `p`, `g`, `pm "name"`).
- Config: existing editor + per-setting help link `https://openkore.com/wiki/<key>` + control-file
  editor (file picker → textarea → save/restore backup).
- More: the Wx menus as button groups — Program (respawn, relog, quit not offered), Info (s, st, i,
  skills, pl, ml, nl, exp, cl, cil, al, vl), Merchant (openshop, closeshop, reload shop),
  Deal (deal, deal no, dealAuto 0/1/2), Party (party, share 0/1, partyAuto 0/1/2, join 1/0, leave),
  Friend/Guild, Settings (ignoreAll, randomWalk, attackAuto, itemsTakeAuto quick toggles,
  reload all), plus Console (existing log + command box).
- NPC talk: when `status.talk` is set, show a modal over any tab with messages and buttons.
- Deal: when `status.deal` is set, show a banner with accept (`deal`) / cancel (`deal no`).
