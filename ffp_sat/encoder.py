from collections import Counter
from pathlib import Path
from statistics import fmean

from .totalizer import AtMost
from .variables import VarManager


class Encoder:
    DEBUG_GROUPS = (
        "initial", "burn_monotonic", "defense_monotonic", "exclusivity",
        "action_definition", "fire_spread", "no_spontaneous_burning",
        "firefighter_totalizer", "containment", "objective_totalizer", "preprocessing",
    )

    def __init__(
        self, instance, firefighters, solver, distance,
        capture_cnf=False, debug=False, capture_clauses=False,
    ):
        self.instance, self.firefighters, self.solver = instance, firefighters, solver
        self.distance = distance
        self.vars = VarManager(capture_names=capture_cnf)
        self.b, self.d, self.a, self.h, self.objectives = {}, {}, {}, {}, {}
        self.horizon = 0
        self.clauses = 0
        self.cnf = [] if capture_cnf or capture_clauses else None
        self.debug = debug
        self._debug_groups = {
            name: {"count": 0, "literals": 0, "min_length": None, "max_length": 0, "auxiliary_vars": 0}
            for name in self.DEBUG_GROUPS
        } if debug else None
        self._clause_histogram = {key: 0 for key in ("1", "2", "3", "4-8", "9-16", ">16")} if debug else None
        self._no_spontaneous_histogram = dict.fromkeys(self._clause_histogram, 0) if debug else None
        self._no_spontaneous_lengths = [] if debug else None
        self.extensions = 0
        self.trees = []
        for v in range(instance.n):
            self.b[v, 0] = self.vars.new(f"b[{v},0]")
            self.d[v, 0] = self.vars.new(f"d[{v},0]")
            self.add([self.b[v, 0] if v in instance.initial_fire else -self.b[v, 0]], "initial")
            self.add([-self.d[v, 0]], "initial")
        self._add_containment(0)

    def add(self, clause, group=None):
        clause = list(clause)
        self.solver.add_clause(clause)
        if self.cnf is not None:
            self.cnf.append(clause)
        self.clauses += 1
        if self._debug_groups is not None:
            if group is None:
                raise AssertionError("Every debug clause must have a profile group")
            profile = self._debug_groups[group]
            length = len(clause)
            profile["count"] += 1
            profile["literals"] += length
            profile["min_length"] = length if profile["min_length"] is None else min(profile["min_length"], length)
            profile["max_length"] = max(profile["max_length"], length)
            bucket = "1" if length == 1 else "2" if length == 2 else "3" if length == 3 else "4-8" if length <= 8 else "9-16" if length <= 16 else ">16"
            self._clause_histogram[bucket] += 1
            if group == "no_spontaneous_burning":
                self._no_spontaneous_histogram[bucket] += 1
                self._no_spontaneous_lengths.append(length)

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
                self.add([-bp, b], "burn_monotonic")
                self.add([-dp, d], "defense_monotonic")
                self.add([-b, -d], "exclusivity")
                for clause in ([-a, d], [-a, -dp], [-d, dp, a]):
                    self.add(clause, "action_definition")
                for u in sorted(self.instance.adjacency[v]):
                    self.add([-self.b[u, t - 1], d, b], "fire_spread")
                self.add([-b, bp] + [self.b[u, t - 1] for u in sorted(self.instance.adjacency[v])], "no_spontaneous_burning")
                if t < self.distance[v]:
                    self.add([-b], "preprocessing")
            aux_before = self.vars.auxiliary
            tree = AtMost([self.a[v, t] for v in range(self.instance.n)], self.firefighters, self.vars)
            if self._debug_groups is not None:
                self._debug_groups["firefighter_totalizer"]["auxiliary_vars"] += self.vars.auxiliary - aux_before
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause, "firefighter_totalizer")
            bound = tree.assumption(self.firefighters)
            if bound is not None:
                self.add([bound], "firefighter_totalizer")
            self._add_containment(t)
        self.horizon = target

    def _add_containment(self, t):
        self.h[t] = self.vars.new(f"h[{t}]", activation=True)
        # A contained state has no edge from a burned vertex to an untouched
        # one. Check each orientation of every undirected edge.
        for u in range(self.instance.n):
            for v in sorted(self.instance.adjacency[u]):
                self.add([-self.h[t], -self.b[u, t], self.b[v, t], self.d[v, t]], "containment")

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
            aux_before = self.vars.auxiliary
            tree = AtMost([self.b[v, t] for v in range(self.instance.n)], upper, self.vars)
            if self._debug_groups is not None:
                self._debug_groups["objective_totalizer"]["auxiliary_vars"] += self.vars.auxiliary - aux_before
            self.objectives[t] = tree
            self.trees.append(tree)
            for clause in tree.clauses:
                self.add(clause, "objective_totalizer")
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

    def debug_profile(self):
        if self._debug_groups is None:
            return None
        clauses = {}
        for name, values in self._debug_groups.items():
            count = values["count"]
            clauses[name] = {
                "number_of_clauses": count,
                "number_of_literals": values["literals"],
                "min_clause_length": values["min_length"],
                "max_clause_length": values["max_length"] if count else None,
                "average_clause_length": values["literals"] / count if count else 0.0,
                "auxiliary_variables": values["auxiliary_vars"],
            }
        totalizers = (
            clauses["firefighter_totalizer"]["number_of_clauses"]
            + clauses["objective_totalizer"]["number_of_clauses"]
        )
        total_clauses = sum(row["number_of_clauses"] for row in clauses.values())
        total_vars = self.vars.top
        return {
            "variables": {
                "semantic": self.vars.semantic,
                "activation": self.vars.activation,
                "auxiliary": self.vars.auxiliary,
                "total": total_vars,
                "auxiliary_ratio": self.vars.auxiliary / total_vars if total_vars else 0.0,
            },
            "clauses": clauses,
            "total_clauses": total_clauses,
            "totalizer_clause_ratio": totalizers / total_clauses if total_clauses else 0.0,
            "clause_length_histogram": dict(self._clause_histogram),
            "no_spontaneous_length_histogram": dict(self._no_spontaneous_histogram),
            "no_spontaneous_exact_length_histogram": {
                str(length): count for length, count in sorted(Counter(self._no_spontaneous_lengths).items())
            },
            "no_spontaneous_clause_length_stats": self._length_stats(self._no_spontaneous_lengths),
        }

    @staticmethod
    def _length_stats(values):
        if not values:
            return {"min": None, "max": None, "mean": 0.0}
        return {"min": min(values), "max": max(values), "mean": fmean(values)}

    def close(self):
        for tree in self.trees:
            tree.close()
