# SAT diagnostics

The ffp_sat.diagnose module runs standalone SAT queries to compare action-prefix
restrictions and horizons. It does not run heuristic or STK search and does not
modify the production solver. Every row runs in a newly spawned process with a
fresh solver instance selected by --solver (CaDiCaL 3.0.0 by default), so learned
clauses cannot pass between rows.

## Fixed-prefix experiment

Pass the result JSON containing a valid incumbent schedule:

```bash
python -m ffp_sat.diagnose fixed-prefix \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule results/incumbent.json --max-prefix 6 \
  --per-query-time 30 --solver cadical300
```

The command solves $F(9,989)$ once for each prefix length from 0 through 6.
prefix_length=0 fixes no actions. Prefix $r$ fixes the first $r$ rounds from
the supplied schedule. With one firefighter, fixing the selected action makes
the round exact because the firefighter cardinality already excludes another
action. When a round uses fewer firefighters than are available, the diagnostic
also fixes all unselected actions false so the prefix remains exact.

The input schedule is checked by the trusted simulator before launching any
case. If the result JSON contains best_k, it must agree with that simulation.
A SAT model is also simulated; its $K$ must satisfy the requested bound and its
containment time must be at most $T$.

## Horizon heatmap

Each requested horizon is solved directly at the same $K$:

```bash
python -m ffp_sat.diagnose heatmap \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --K 989 --horizons 9 11 13 15 18 \
  --per-query-time 30 --solver cadical300
```

The command does not run preceding $K$ bounds or reuse an earlier horizon's
solver. Each row encodes only its requested horizon, creates containment at
that horizon, and applies the saved-side objective threshold $n-K$.

## Output fields and timeout behavior

By default, files go directly to diagnostics/ as fixed_prefix.json/csv or
horizon_heatmap.json/csv. Supply --out-dir to choose another directory. Both
modes write a JSON file with run metadata and all cases, plus a CSV with one row
per case.

Each case records experiment, prefix_length where applicable, $T$, $K$,
saved_required, result, encoding and solve times, variable and clause counts,
assumption count, and worker_pid. CaDiCaL decisions, conflicts, propagations,
and restarts are included when available after a completed solve. SAT rows
include the simulator-validated actual_k, containment time, and normalized
schedule. Fixed-prefix rows list selected actions as [round, vertex].

--per-query-time is a hard wall-clock budget per spawned case, including input
reading, preprocessing, encoding, solving, and SAT schedule validation. If that
deadline arrives while a query is running, the parent terminates its worker and
records result=TIMEOUT; an interrupted call is never interpreted as UNSAT.
For such a timeout, solve time is estimated from the worker's query-start
message. Encoding counts are checkpointed after encoding completes. A worker
crash is reported as ERROR.

When adding a fixed prefix makes an UNSAT query much faster, the prefix has
removed difficult action choices or their associated branching. If $F(T,K)$
times out at shorter horizons but becomes SAT or UNSAT quickly at larger
horizons, the short horizon is a source of search difficulty. If all horizons
remain difficult, the results do not support horizon tightness as the main
cause. These experiments provide evidence only; they do not change solver
settings or prove that the same behavior holds for other instances.

## Phase guidance

Check phase preferences on one fixed query without changing the production STK
solver:

```bash
python -m ffp_sat.diagnose phase-guidance \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule results/incumbent.json \
  --modes none action full consensus \
  --consensus-pool-size 32 --seed 0 \
  --per-query-time 60 --solver cadical300
```

The command first checks that the selected PySAT backend exposes
`set_phases()`. Each mode then runs in its own spawned process with a fresh
CaDiCaL instance. `none` does not call `set_phases`; `action` prefers every
action variable according to the incumbent; `full` also prefers burned and
defended semantic state variables from the simulator trace; `consensus` votes
for actions by round across the incumbent, Pareto schedules, and a seeded pool
of randomized threat schedules. These preferences do not add clauses or
assumptions. Every SAT schedule is independently checked by the simulator.

The JSON and CSV output include phase literal counts and polarity counts,
guidance construction time, solve time, assumptions, and the CNF size. They
also include CaDiCaL decisions, conflicts, propagations, and restarts when the
backend reports them. The command checks that all modes have identical CNF
counts and assumptions. Consensus output records pool size and the winning
vertex votes by round. Completed solves also report decisions per second,
conflicts per decision, and propagations per decision when those statistics
are available.

To measure whether explicit phase preferences help after CaDiCaL has already
processed the previous production bounds, add a replay:

```bash
python -m ffp_sat.diagnose phase-guidance \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule results/incumbent.json \
  --modes none action full consensus \
  --replay-bounds 991 990 \
  --per-query-time 120 --solver cadical300
```

Each mode still has a fresh solver. Within that solver, replay bounds are
solved normally in the listed descending order; phase guidance is applied only
before the final query. Statistics for that query are reported as deltas from
immediately before its solve. The per-query wall-clock limit covers the whole
replay case, including setup and earlier bounds. Replay output is written to
`phase_guidance_replay.json` and `.csv`; direct output uses
`phase_guidance.json` and `.csv`.
