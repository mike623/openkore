"""Sync OpenKore tables/portals.txt walk portals with the rAthena Renewal server's warp NPCs.
usage: python3 warpsync.py [apply]"""
import re, sys
from pathlib import Path
R = Path.home() / ".ragnarokoffline/rathena-src"
P = Path.home() / ".ragnarokoffline/openkore-probe/openkore/tables/portals.txt"

files = []
for conf in ("npc/scripts_warps.conf", "npc/re/scripts_warps.conf"):
    for line in (R / conf).read_text().splitlines():
        m = re.match(r"\s*npc:\s*(\S+)", line)
        if m: files.append(R / m.group(1))
warps = {}  # name -> (src, x, y, dst, dx, dy); later files override
W = re.compile(r"^([a-z0-9_@-]+),(\d+),(\d+),\d+\s+warp2?\s+(\S+)\s+\d+,\d+,([a-z0-9_@-]+),(\d+),(\d+)", re.I)
for f in files:
    if not f.exists(): continue
    for line in f.read_text("utf-8", "replace").splitlines():
        m = W.match(line)
        if m:
            s, x, y, name, d, dx, dy = m.groups()
            warps[name] = (s, int(x), int(y), d, int(dx), int(dy))
srv = {}
for w in warps.values(): srv.setdefault(w[0], []).append(w)

lines = P.read_text("utf-8", "surrogateescape").split("\n")
L = re.compile(r"^([a-z0-9_@-]+) (\d+) (\d+) ([a-z0-9_@-]+) (\d+) (\d+)\s*$", re.I)  # plain walk portals only
near = lambda a, b: abs(a[0]-b[0]) <= 4 and abs(a[1]-b[1]) <= 4
out, stale, have, moved = [], [], set(), []
for line in lines:
    m = L.match(line)
    if m and m.group(1) in srv:
        s, x, y, d, dx, dy = m.groups(); x, y = int(x), int(y)
        dx, dy = int(dx), int(dy)
        hit = [w for w in srv[s] if w[3] == d and near((x, y), w[1:3]) and near((dx, dy), w[4:6])]
        if hit:
            w = hit[0]; have.add(w)
            if abs(x - w[1]) > 1 or abs(y - w[2]) > 1:  # warp span is ~1 cell: 4 off never triggers -> snap
                moved.append(f"{line}  ->  {w[1]} {w[2]}")
                out.append(f"{s} {w[1]} {w[2]} {d} {dx} {dy}"); continue
            out.append(line); continue
        stale.append(line); out.append("# rooff-stale " + line); continue
    out.append(line)
missing = [w for ws in srv.values() for w in ws if w not in have]
print(f"server warps {len(warps)}, ok {len(have)}, moved {len(moved)}, stale lines {len(stale)}, missing {len(missing)}")
for m in moved[:40]: print("  moved  ", m)
for s in stale[:15]: print("  stale  ", s)
for w in [w for w in missing if w[0] in ('comodo','beach_dun3','cmd_fild01','cmd_fild02','izlude','prontera','morocc')][:20]: print("  missing", *w)
if sys.argv[1:] == ["apply"]:
    out.append("# rooff: walk portals from this server's warp NPCs (warpsync.py)")
    out += [f"{s} {x} {y} {d} {dx} {dy}" for s, x, y, d, dx, dy in missing]
    P.write_text("\n".join(out), "utf-8", "surrogateescape")
    print("applied")
