"""Audit OpenKore tables/portals.txt against the rAthena Renewal server this bot plays on.

    python3 portal_audit.py            report only
    python3 portal_audit.py apply      comment out stale NPC portals, rebuild mismatched fields

Checks: NPC portal lines (with talk steps) need a server NPC within 4 cells of the source;
every arrival cell must be walkable (within 3 cells); every map's .fld2 must match the
server's .gat size (served by the app's asset server). Walk portals: run warpsync.py.
"""
import gzip, re, struct, sys, urllib.request
from pathlib import Path

R = Path.home() / ".ragnarokoffline/rathena-src"
K = Path(__file__).resolve().parents[2]
P = K / "tables/portals.txt"
FIELDS = K / "fields"
ASSETS = "http://127.0.0.1:3338/data/"
APPLY = sys.argv[1:] == ["apply"]


def scripts():
    seen, out = set(), []
    def walk(conf):
        if conf in seen or not conf.exists():
            return
        seen.add(conf)
        for line in conf.read_text("utf-8", "replace").splitlines():
            m = re.match(r"\s*(import|npc):\s*(\S+)", line)
            if m:
                (walk if m.group(1) == "import" else out.append)(R / m.group(2))
    walk(R / "npc/re/scripts_main.conf")
    return out


npcs = {}  # map -> [(x, y, name)]
HEAD = re.compile(r"^([a-z0-9_@-]+),(\d+),(\d+),\d+\t(script|duplicate\([^)]*\)|shop|warp2?)\t([^\t]+)")
for f in scripts():
    if f.exists():
        for line in f.read_text("utf-8", "replace").splitlines():
            m = HEAD.match(line)
            if m:
                npcs.setdefault(m.group(1), []).append((int(m.group(2)), int(m.group(3)), m.group(5)))

_fields = {}
def field(m):
    if m not in _fields:
        f = next((p for p in (FIELDS / f"{m}.fld2", FIELDS / f"{m}.fld2.gz") if p.exists()), None)
        if f is None:
            _fields[m] = None
        else:
            d = (gzip.open if f.suffix == ".gz" else open)(f, "rb").read()
            w, h = struct.unpack("<HH", d[:4])
            _fields[m] = (w, h, d[4:], f)
    return _fields[m]

def server_gat(m):
    try:
        return urllib.request.urlopen(ASSETS + m + ".gat", timeout=10).read()
    except Exception:
        return None

def walkable_near(m, x, y, r=3):
    fd = field(m)
    if not fd:
        return None
    w, h, c, _ = fd
    return any(0 <= x + dx < w and 0 <= y + dy < h and c[(y + dy) * w + x + dx] & 1
               for dx in range(-r, r + 1) for dy in range(-r, r + 1))

# gat cell type -> fld2 bits (1 walk, 2 snipe, 4 water, 8 cliff); learned from matching maps
GAT2FLD = {0: 1, 1: 0, 2: 1, 3: 5, 4: 1, 5: 10, 6: 1}
def gat_to_fld2(gat):
    w, h = struct.unpack("<II", gat[6:14])
    cells = bytearray(GAT2FLD.get(struct.unpack_from("<I", gat, 14 + i * 20 + 16)[0], 0) for i in range(w * h))
    return struct.pack("<HH", w, h) + bytes(cells)


L = re.compile(r"^([a-z0-9_@-]+) (\d+) (\d+) ([a-z0-9_@-]+) (\d+) (\d+)(.*)$", re.I)
lines = P.read_text("utf-8", "surrogateescape").split("\n")
maps, stale_npc, bad_dest, out = set(), [], [], []
for line in lines:
    m = L.match(line)
    if not m:
        out.append(line); continue
    s, x, y, d, dx, dy, rest = m.groups(); x, y, dx, dy = map(int, (x, y, dx, dy))
    maps |= {s, d}
    steps = re.sub(r"^\s*\d+(\s+\d+)?", "", rest.split("#")[0]).strip()
    if steps and s in npcs and not any(abs(nx - x) <= 4 and abs(ny - y) <= 4 for nx, ny, _ in npcs[s]):
        stale_npc.append(line); out.append("# rooff-stale-npc " + line); continue
    if walkable_near(d, dx, dy) is False:
        bad_dest.append(line)
    out.append(line)

size_bad, missing_fld = [], []
for mp in sorted(maps):
    gat = server_gat(mp)
    if not gat or gat[:4] != b"GRAT":
        continue
    sw, sh = struct.unpack("<II", gat[6:14])
    fd = field(mp)
    if fd is None:
        missing_fld.append(mp)
    elif (fd[0], fd[1]) != (sw, sh):
        size_bad.append(f"{mp} local {fd[0]}x{fd[1]} server {sw}x{sh}")
    else:
        continue
    if APPLY:
        new = gat_to_fld2(gat)
        if fd:
            fd[3].rename(fd[3].with_name(fd[3].name + ".pre-audit"))
        (FIELDS / f"{mp}.fld2").write_bytes(new)
        _fields.pop(mp, None)

print(f"lines {len(lines)}, maps {len(maps)}")
print(f"stale NPC portals (no server NPC near source): {len(stale_npc)}")
for s in stale_npc[:25]: print("  ", s)
print(f"arrival cell not walkable: {len(bad_dest)}")
for s in bad_dest[:25]: print("  ", s)
print(f"field size mismatch: {len(size_bad)}  missing field: {len(missing_fld)}")
for s in size_bad[:40]: print("  ", s)
if missing_fld: print("   missing:", " ".join(missing_fld[:40]))
if APPLY:
    P.write_text("\n".join(out), "utf-8", "surrogateescape")
    print("applied (portals.txt + fields); now: portals recompile")
