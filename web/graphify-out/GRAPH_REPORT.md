# Graph Report - web  (2026-11-07)

## Corpus Check
- 3 files · ~28,479 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 137 nodes · 370 edges · 12 communities (11 shown, 1 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 7 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `c422b1d8`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- aria.py
- kore-gateway.py
- asyncio
- Request
- selftest
- prefer_ids
- Kore web panel — contracts
- re
- run_tool
- read_config
- server_scripts
- ai_finish

## God Nodes (most connected - your core abstractions)
1. `selftest()` - 28 edges
2. `run_tool()` - 20 edges
3. `authed()` - 15 edges
4. `config_post()` - 11 edges
5. `ai_data()` - 11 edges
6. `config_lines()` - 9 edges
7. `write_backed_up()` - 9 edges
8. `prefer_ids()` - 9 edges
9. `read_config()` - 8 edges
10. `config_get()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `AI advisor (`POST /kore/chat`)` --references--> `kore_command()`  [INFERRED]
  CONTRACT.md → kore-gateway.py
- `AI advisor (`POST /kore/chat`)` --references--> `find_leveling_maps()`  [INFERRED]
  CONTRACT.md → kore-gateway.py
- `AI advisor (`POST /kore/chat`)` --references--> `get_config()`  [INFERRED]
  CONTRACT.md → kore-gateway.py
- `AI advisor (`POST /kore/chat`)` --references--> `server_npcs()`  [INFERRED]
  CONTRACT.md → kore-gateway.py
- `AI advisor (`POST /kore/chat`)` --references--> `read_log()`  [INFERRED]
  CONTRACT.md → kore-gateway.py

## Import Cycles
- None detected.

## Communities (12 total, 1 thin omitted)

### Community 0 - "aria.py"
Cohesion: 0.15
Nodes (9): Brain, guard(), kore(), main(), listen(), watch_level(), parse(), selftest() (+1 more)

### Community 1 - "kore-gateway.py"
Cohesion: 0.17
Nodes (9): ai_companions(), backup_names(), config_get(), config_lines(), exchange(), mask(), unmask(), write_backed_up() (+1 more)

### Community 2 - "asyncio"
Cohesion: 0.15
Nodes (9): bus_decode(), bus_encode(), cli_command(), console(), downstream(), upstream(), kore_capture(), panel() (+1 more)

### Community 3 - "Request"
Cohesion: 0.30
Nodes (13): authed(), chat_post(), config_post(), file_get(), file_post(), files_get(), kore_command(), map_get() (+5 more)

### Community 4 - "selftest"
Cohesion: 0.27
Nodes (7): bmp8_png(), check_raw(), field_png(), kore_readonly(), load_field_png(), png_chunk(), selftest()

### Community 5 - "prefer_ids"
Cohesion: 0.20
Nodes (5): hp_percent(), item_ids(), name_table(), prefer_ids(), skill_id()

### Community 6 - "Kore web panel — contracts"
Cohesion: 0.25
Nodes (7): Auth (existing, unchanged), Existing routes (unchanged), Kore web panel — contracts, New routes (gateway), status.json (plugin, written every ~1 s, atomic), Store list, UI (panel) — Wx parity on a phone

### Community 7 - "re"
Cohesion: 0.38
Nodes (5): AI advisor (`POST /kore/chat`), control_file(), parse_skill_descriptions(), read_control_file(), read_log()

### Community 8 - "run_tool"
Cohesion: 0.52
Nodes (6): ai_character(), ai_data(), ai_skills(), find_leveling_maps(), _mob(), run_tool()

### Community 9 - "read_config"
Cohesion: 0.33
Nodes (4): block_clash(), block_values(), get_config(), read_config()

### Community 10 - "server_scripts"
Cohesion: 0.40
Nodes (3): npc_index(), server_npcs(), server_scripts()

## Knowledge Gaps
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AI advisor (`POST /kore/chat`)` connect `re` to `Request`, `Kore web panel — contracts`, `run_tool`, `read_config`, `server_scripts`?**
  _High betweenness centrality (0.112) - this node is a cross-community bridge._
- **Should `aria.py` be split into smaller, more focused modules?**
  _Cohesion score 0.1471861471861472 - nodes in this community are weakly interconnected._
- **Why does `Kore web panel — contracts` connect `Kore web panel — contracts` to `re`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Should `asyncio` be split into smaller, more focused modules?**
  _Cohesion score 0.14705882352941177 - nodes in this community are weakly interconnected._
- **Why does `selftest()` connect `selftest` to `kore-gateway.py`, `asyncio`, `prefer_ids`, `re`, `run_tool`, `read_config`, `server_scripts`, `ai_finish`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._