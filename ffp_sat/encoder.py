from pathlib import Path

from .totalizer import AtMost
from .variables import VarManager


class Encoder:
    def __init__(self, instance, firefighters, solver, distance, capture_cnf=False):
        self.instance, self.firefighters, self.solver = instance, firefighters, solver
        self.distance = distance
        self.vars = VarManager(capture_names=capture_cnf)
        self.b, self.d, self.a, self.h, self.objectives = {}, {}, {}, {}, {}
        self.horizon = 0
        self.clauses = 0
        self.cnf = [] if capture_cnf else None
        self.extensions = 0
        self.trees = []
        for v in range(instance.n):
            self.b[v, 0] = self.vars.new(f"b[{v},0]")
            self.d[v, 0] = self.vars.new(f"d[{v},0]")
            self.add([self.b[v, 0] if v in instance.initial_fire else -self.b[v, 0]])
            self.add([-self.d[v, 0]])
        self._add_containment(0)

    def add(self, clause):
        clause = list(clause)
        self.solver.add_clause(clause)
        if self.cnf is not None:
            self.cnf.append(clause)
        self.clauses += 1

    def ensure_horizon(self, target):
        if target < 0:
            raise ValueError("Horizon must be non-negative")
        if target <= self.horizon:
            return
        self.extensions += 1
        for t in range(self.horizon + 1, target + 1):
            for v in range(self.instance.n):
                self.b[v, t] = self.vars.new(f"b[{v},{t}]")
                self.d[v, t] = self.vars.new(f"d[{v},{t}]")
                self.a[v, t] = self.vars.new(f"a[{v},{t}]")
            for v in range(self.instance.n):
                b, bp, d, dp, a = self.b[v, t], self.b[v, t - 1], self.d[v, t], self.d[v, t - 1], self.a[v, t]
                for clause in ([-bp, b], [-dp, d], [-b, -d], [-a, d], [-a, -dp], [-d, dp, a]):
                    self.add(clause)
                for u in sorted(self.instance.adjacency[v]):
                    self.add([-self.b[u, t - 1], d, b])
                self.add([-b, bp] + [self.b[u, t - 1] for u in sorted(self.instance.adjacency[v])])
                if t < self.distance[v]:
                    self.add([-b])
            tree = AtMost([self.a[v, t] for v in range(self.instance.n)], self.firefighters, self.vars)
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause)
            bound = tree.assumption(self.firefighters)
            if bound is not None:
                self.add([bound])
            self._add_containment(t)
        self.horizon = target

    def _add_containment(self, t):
        self.h[t] = self.vars.new(f"h[{t}]", activation=True)
        # A contained state has no edge from a burned vertex to an untouched
        # one. Check each orientation of every undirected edge.
        for u in range(self.instance.n):
            for v in sorted(self.instance.adjacency[u]):
                self.add([-self.h[t], -self.b[u, t], self.b[v, t], self.d[v, t]])

    def export_dimacs(self, prefix, assumptions, horizon, burned_bound):
        """Write this query's cumulative CNF, including its active assumptions."""
        if self.cnf is None or self.vars.names is None:
            raise RuntimeError("CNF export requires capture_cnf=True")
        prefix = Path(prefix)
        if prefix.suffix.lower() == ".cnf":
            prefix = prefix.with_suffix("")
        prefix.parent.mkdir(parents=True, exist_ok=True)
        raw_path = prefix.with_suffix(".cnf")
        named_path = prefix.with_name(prefix.name + ".named.cnf")
        clauses = [*self.cnf, *([literal] for literal in assumptions)]
        top = self.vars.top
        header = f"p cnf {top} {len(clauses)}\n"

        def clause_line(clause):
            return " ".join(map(str, clause)) + (" " if clause else "") + "0\n"

        with raw_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(header)
            stream.writelines(clause_line(clause) for clause in clauses)
        with named_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"c FFP query horizon={horizon} burned_at_most={burned_bound}\n")
            for variable in range(1, top + 1):
                stream.write(f"c var {variable} {self.vars.names[variable]}\n")
            stream.write(header)
            stream.writelines(clause_line(clause) for clause in clauses)
        return dict(raw=str(raw_path), named=str(named_path))

    def assumptions(self, t, k, upper):
        if t not in self.objectives:
            tree = AtMost([self.b[v, t] for v in range(self.instance.n)], upper, self.vars)
            self.objectives[t] = tree
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause)
        bound = self.objectives[t].assumption(k)
        return [self.h[t]] + ([bound] if bound is not None else [])

    def decode(self, model, t):
        positive = {v for v in model if v > 0}
        return [
            tuple(v for v in range(self.instance.n) if self.a[v, step] in positive)
            for step in range(1, t + 1)
        ]

    def stats(self):
        return dict(
            n_semantic_vars=self.vars.semantic,
            n_activation_vars=self.vars.activation,
            n_aux_vars=self.vars.auxiliary,
            n_clauses=self.clauses,
            number_of_horizon_extensions=self.extensions,
        )

    def close(self):
        for tree in self.trees:
            tree.close()
