from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Instance:
    adjacency: tuple[frozenset[int], ...]
    initial_fire: frozenset[int]
    seed: int = 0
    description: str = ""
    name: str = ""

    @property
    def n(self):
        return len(self.adjacency)

    @property
    def m(self):
        return sum(map(len, self.adjacency)) // 2


def read_instance(path):
    lines = [s.strip() for s in Path(path).read_text().splitlines() if s.strip()]
    if len(lines) < 6:
        raise ValueError("Instance requires six header lines")
    seed, n, m = map(int, lines[:3])
    count = int(lines[4])
    vertices = list(map(int, lines[5].split()))
    if n < 1 or m < 0 or count < 1 or len(vertices) != count or len(set(vertices)) != count:
        raise ValueError("Invalid graph size or initial fire set")
    if any(v < 0 or v >= n for v in vertices):
        raise ValueError("Initial fire vertex out of range")
    if len(lines) != 6 + m:
        raise ValueError("Edge count does not match declaration")
    adjacency = [set() for _ in range(n)]
    for line in lines[6:]:
        edge = list(map(int, line.split()))
        if len(edge) != 2:
            raise ValueError("Each edge requires two vertex IDs")
        u, v = edge
        if not (0 <= u < n and 0 <= v < n) or u == v or v in adjacency[u]:
            raise ValueError("Invalid, duplicate, or self-loop edge")
        adjacency[u].add(v)
        adjacency[v].add(u)
    return Instance(tuple(map(frozenset, adjacency)), frozenset(vertices), seed, lines[3], str(path))
