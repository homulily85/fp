"""Action-only timeline canonicalization for SAT diagnostics."""

from .objective import IncrementalAtLeastCounter


class ActionCanonicalEncoding:
    """Append exact action indicators and optional prefix constraints.

    Any later defense can be moved into an earlier unused firefighter slot:
    its vertex was untouched before its original defense round and remains
    untouched when earlier defenses only reduce fire spread. Earlier defense
    cannot increase the burned set or delay containment. Repeating this move
    yields a schedule whose action counts are full, then possibly partial, then
    zero. This is an existence-preserving restriction for the objective, not a
    property of every feasible schedule.
    """

    def __init__(self, encoder, firefighters):
        self.encoder = encoder
        self.firefighters = firefighters
        self.y = {}
        self.indicator_clauses = 0
        self.prefix_clauses = 0
        self.full_capacity_clauses = 0
        self.full_capacity_auxiliary_variables = 0
        self.full_capacity_counters = {}

    def _add(self, clause, counter):
        self.encoder.solver.add_clause(clause)
        setattr(self, f"{counter}_clauses", getattr(self, f"{counter}_clauses") + 1)

    def add_indicators(self, horizon):
        for t in range(1, horizon + 1):
            if t in self.y:
                continue
            y = self.encoder.vars.new_aux(f"action_present[{t}]")
            self.y[t] = y
            for v in range(self.encoder.instance.n):
                self._add([-self.encoder.a[v, t], y], "indicator")
            self._add(
                [-y, *[self.encoder.a[v, t] for v in range(self.encoder.instance.n)]],
                "indicator",
            )

    def add_prefix_rules(self, horizon):
        self.add_indicators(horizon)
        for t in range(1, horizon):
            self._add([-self.y[t + 1], self.y[t]], "prefix")

    def add_full_capacity_rules(self, horizon):
        self.add_indicators(horizon)
        if self.firefighters <= 1:
            return
        threshold = min(self.firefighters, self.encoder.instance.n)
        for t in range(1, horizon):
            if t in self.full_capacity_counters:
                continue
            counter = IncrementalAtLeastCounter(
                [self.encoder.a[v, t] for v in range(self.encoder.instance.n)],
                self.encoder.vars,
                lambda clause: self._add(clause, "full_capacity"),
                f"action_round_{t}",
            )
            before = self.encoder.vars.auxiliary
            threshold_literal = counter.assumption(threshold)
            self.full_capacity_auxiliary_variables += self.encoder.vars.auxiliary - before
            self.full_capacity_counters[t] = counter
            # If round t+1 contains an action, round t is non-final and must
            # use full capacity. The base encoder already imposes AtMost-D.
            self._add([-self.y[t + 1], threshold_literal], "full_capacity")

    def apply_mode(self, mode, horizon):
        if mode == "base":
            return
        self.add_indicators(horizon)
        if mode in {"prefix", "canonical"}:
            self.add_prefix_rules(horizon)
        if mode == "canonical":
            self.add_full_capacity_rules(horizon)

    def stats(self):
        return {
            "action_indicator_variables": len(self.y),
            "indicator_clauses": self.indicator_clauses,
            "prefix_clauses": self.prefix_clauses,
            "full_capacity_clauses": self.full_capacity_clauses,
            "full_capacity_auxiliary_variables": self.full_capacity_auxiliary_variables,
        }
