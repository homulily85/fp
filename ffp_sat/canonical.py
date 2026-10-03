"""Optional active-state and canonical-action clauses for diagnostics only."""

from .objective import IncrementalAtLeastCounter


class CanonicalActionEncoding:
    """Append exact active-state and selected canonical-action constraints.

    These are existence-preserving restrictions for the FFP objective: after
    containment, actions can be removed; while fire is active, an idle round can
    be changed to protect a threatened vertex; and an active non-final state
    can be extended to use all available firefighters without worsening fire
    spread. They are deliberately kept out of the production STK encoder.
    """

    def __init__(self, encoder):
        self.encoder = encoder
        self.active = {}
        self.witness = {}
        self.active_horizon = -1
        self.active_state_clauses = 0
        self.stop_clauses = 0
        self.nonempty_clauses = 0
        self.full_capacity_clauses = 0
        self.full_capacity_auxiliary_variables = 0
        self._full_capacity_counters = {}

    def _add(self, clause, group):
        self.encoder.solver.add_clause(clause)
        setattr(self, f"{group}_clauses", getattr(self, f"{group}_clauses") + 1)

    def _aux(self, name):
        return self.encoder.vars.new_aux(name)

    def ensure_active_states(self, last_t):
        if last_t < 0:
            return
        if last_t <= self.active_horizon:
            return
        for t in range(self.active_horizon + 1, last_t + 1):
            witnesses = []
            for v in range(self.encoder.instance.n):
                neighbors = sorted(self.encoder.instance.adjacency[v])
                if not neighbors:
                    continue
                q = self._aux(f"active_witness[{v},{t}]")
                self.witness[v, t] = q
                witnesses.append(q)
                b, d = self.encoder.b[v, t], self.encoder.d[v, t]
                self._add([-q, -b], "active_state")
                self._add([-q, -d], "active_state")
                self._add([-q, *[self.encoder.b[u, t] for u in neighbors]], "active_state")
                for u in neighbors:
                    self._add([-self.encoder.b[u, t], b, d, q], "active_state")

            g = self._aux(f"active[{t}]")
            self.active[t] = g
            for q in witnesses:
                self._add([-q, g], "active_state")
            self._add([-g, *witnesses], "active_state")
            if t > 0:
                self._add([-g, self.active[t - 1]], "active_state")
        self.active_horizon = last_t

    def add_stop_after_contained(self, horizon):
        self.ensure_active_states(horizon - 1)
        for t in range(1, horizon + 1):
            g_previous = self.active[t - 1]
            for v in range(self.encoder.instance.n):
                self._add([-self.encoder.a[v, t], g_previous], "stop")

    def add_active_nonempty(self, horizon):
        self.ensure_active_states(horizon - 1)
        for t in range(1, horizon + 1):
            self._add(
                [-self.active[t - 1], *[self.encoder.a[v, t] for v in range(self.encoder.instance.n)]],
                "nonempty",
            )

    def add_full_capacity_nonfinal(self, horizon, firefighters):
        self.ensure_active_states(horizon - 1)
        if firefighters <= 1:
            return
        threshold = min(firefighters, self.encoder.instance.n)
        for t in range(1, horizon):
            counter = IncrementalAtLeastCounter(
                [self.encoder.a[v, t] for v in range(self.encoder.instance.n)],
                self.encoder.vars,
                lambda clause: self._add(clause, "full_capacity"),
                f"canonical_round_{t}",
            )
            before = self.encoder.vars.auxiliary
            at_least = counter.assumption(threshold)
            self.full_capacity_auxiliary_variables += self.encoder.vars.auxiliary - before
            self._full_capacity_counters[t] = counter
            self._add([-self.active[t], at_least], "full_capacity")

    def apply_mode(self, mode, horizon, firefighters):
        if mode == "base":
            return
        self.ensure_active_states(horizon - 1)
        if mode in {"stop-after-contained", "canonical"}:
            self.add_stop_after_contained(horizon)
        if mode == "canonical":
            self.add_active_nonempty(horizon)
            self.add_full_capacity_nonfinal(horizon, firefighters)

    def stats(self):
        return {
            "active_variables": len(self.active),
            "threat_witness_variables": len(self.witness),
            "active_state_clauses": self.active_state_clauses,
            "stop_clauses": self.stop_clauses,
            "nonempty_clauses": self.nonempty_clauses,
            "full_capacity_clauses": self.full_capacity_clauses,
            "full_capacity_auxiliary_variables": self.full_capacity_auxiliary_variables,
        }
