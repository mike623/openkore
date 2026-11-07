"""Places the bot can reach from a town but not return from, using OpenKore's own data:
tables/portals.txt (edges) and tables/portalsLOS.txt (which portals are walkable from each arrival).
usage: python3 return_audit.py [town]   (default izlude)"""
import re, sys
from collections import defaultdict, deque
from pathlib import Path

T = Path(__file__).resolve().parents[1]
town = (sys.argv[1:] or ["izlude"])[0]
L = re.compile(r"^([a-z0-9_@-]+) (\d+) (\d+) ([a-z0-9_@-]+) (\d+) (\d+)(.*)$", re.I)
portal = defaultdict(list)            # source point -> [(dest point, is_npc)]
for line in (T / "portals.txt").read_text("utf-8", "replace").splitlines():
    m = L.match(line)
    if m:
        s, x, y, d, dx, dy, rest = m.groups()
        portal[(s, int(x), int(y))].append(((d, int(dx), int(dy)), bool(re.search(r"[a-z]", rest.split("#")[0]))))
walk = defaultdict(set)               # arrival point -> portal sources walkable from it
for line in (T / "portalsLOS.txt").read_text("utf-8", "replace").splitlines():
    p = line.split()
    if len(p) >= 3:
        a = (p[0], int(p[1]), int(p[2]))
        for i in range(3, len(p) - 3, 4):
            if p[i + 3] != "0":
                walk[a].add((p[i], int(p[i + 1]), int(p[i + 2])))
nodes = set(walk) | {d for v in portal.values() for d, _ in v}
by_map = defaultdict(list)
for src in portal: by_map[src[0]].append(src)
for a in nodes:   # arriving on (or next to) a portal: OpenKore's LOS table leaves that one out
    walk[a] |= {s for s in by_map[a[0]] if abs(s[1] - a[1]) <= 2 and abs(s[2] - a[2]) <= 2}
edges = defaultdict(set)
for a in nodes:
    for src in walk.get(a, ()):
        for d, _ in portal.get(src, ()):
            edges[a].add(d)
rev = defaultdict(set)
for a, ds in edges.items():
    for d in ds: rev[d].add(a)
starts = [n for n in nodes if n[0] == town]
def bfs(seed, g):
    seen, q = set(seed), deque(seed)
    while q:
        for n in g[q.popleft()]:
            if n not in seen: seen.add(n); q.append(n)
    return seen
reach, back = bfs(starts, edges), bfs(starts, rev)
stuck = sorted({n[0] for n in reach - back})
# maps where SOME arrivals return but others don't are partial (often fine: separate rooms)
full = [m for m in stuck if not any(n[0] == m and n in back for n in reach)]
print(f"reachable maps from {town}: {len({n[0] for n in reach})}; no way back from: {len(full)}")
print(" ".join(full))
