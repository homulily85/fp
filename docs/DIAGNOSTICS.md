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

## Canonical action ablation

Compare the base query with active-state and action canonicalization clauses:

```bash
python -m ffp_sat.diagnose canonical-actions \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --modes base active-only stop-after-contained canonical \
  --per-query-time 30 --solver cadical300
```

Every mode uses a fresh process and CaDiCaL instance. The base encoder first
builds the complete query, including objective and containment assumptions.
Optional active-state and canonical clauses are appended afterward, so base
variable IDs, clauses, and assumptions remain identical.

`active-only` defines `g[t]` as whether a burned vertex has an untouched
neighbor. It uses one witness per non-isolated vertex and an OR over witnesses;
it does not change action rules. `stop-after-contained` additionally forbids
actions in rounds whose previous state was already contained. `canonical`
also requires an action while fire is active and, for firefighter counts above
one, requires full capacity in rounds that remain active after that round.
These restrictions select canonical representatives that preserve existence
for the objective; they do not describe every feasible schedule.

The result reports the base and total formula sizes separately, plus active
and witness variable counts, clauses by canonical rule, canonical encoding
time, and SAT statistics when the solve completes. SAT schedules are checked
with the simulator. With one firefighter, the existing at-most-one constraint
and the active nonempty rule already imply exactly one action in each active
round, so no additional full-capacity counter is built.

## Action timeline ablation

Test a lighter canonicalization that adds only action-presence indicators:

```bash
python -m ffp_sat.diagnose action-canonical \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --modes base indicator-only prefix canonical \
  --per-query-time 60 --solver cadical300
```

For each round, `y[t]` is equivalent to at least one action in that round.
`indicator-only` adds only these equivalences. `prefix` adds `y[t+1] -> y[t]`,
which rules out idle gaps before a later action. `canonical` also requires a
previous round to use all $D$ firefighters if the following round has an
action. It uses the incremental AtLeast counter for $D>1$ and skips that
counter for $D=1$, where prefix plus the existing AtMost-one constraint already
has the full-capacity effect. The canonical schedule restriction follows by
moving later defenses into earlier free slots; this can only reduce fire
spread and cannot make a previously defended vertex unavailable earlier.

The output separates the base formula from indicator, prefix, and
full-capacity overhead. With $T=9$, `indicator-only` should add 9 variables and
9,009 clauses; `prefix` adds eight more clauses. For $D=1$, `canonical` has the
same counts as `prefix`.

### Incremental replay styles

Replay the same solver through earlier bounds using separate hard solve
budgets for replay and final stages:

```bash
python -m ffp_sat.diagnose action-canonical \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --modes base indicator-only prefix \
  --replay-bounds 991 990 --replay-style late-append \
  --replay-query-time 90 --final-query-time 120 --solver cadical300
```

`integrated` adds the selected action clauses after building the first base
query and keeps them active for every bound. `late-append` solves all replay
bounds with BASE, creates the final objective assumptions, records the exact
pre-canonical formula and assumptions, then appends the action clauses before
the final solve. In late-append mode, the JSON reports `history_mismatch` if
earlier results, CDCL statistic deltas, formula sizes, or final pre-append
assumptions differ between modes. It separately reports
`canonical_equivalence_mismatch` if a completed SAT/UNSAT result differs from
BASE.

Each solve stage gets its own parent-enforced wall-clock timeout. A replay
stage timeout ends that mode with `REPLAY_TIMEOUT`; a final-stage timeout is
`TIMEOUT`. An interrupted solve is never recorded as UNSAT. SAT models from
every completed stage are checked by the simulator. Replay CSV output has one
row per stage, while JSON groups stage details under each mode. Output files
are named `action_canonical_integrated_replay.*` and
`action_canonical_late_append_replay.*`. Pass `--output-stem` to keep runs
with different budgets separate, for example
`--output-stem action_canonical_late_append_600s`.

### Prefix core mining

Check PySAT/CaDiCaL assumption-core support and probe known depth-3/depth-5
prefixes first:

```bash
python -m ffp_sat.diagnose prefix-core-mining \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule /tmp/ffp-diagnostic-incumbent.json \
  --sanity-depths 3 5 --probe-time 10 --solver cadical300
```

For full mining and master comparison, pass replay bounds. Query assumptions
are asserted as unit clauses inside each fresh probe worker, so `get_core()`
contains only prefix assumptions. Cores are returned as semantic
`(round, vertex, polarity)` triples; master clauses are guarded by the final
query assumptions before they are added. A timeout never contributes a core.

```bash
python -m ffp_sat.diagnose prefix-core-mining \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule /tmp/ffp-diagnostic-incumbent.json \
  --pool-size 128 --prefix-depth 5 --probe-time 5 --seed 0 --jobs 1 \
  --replay-bounds 991 990 --replay-query-time 90 \
  --final-query-time 600 --solver cadical300
```

The JSON report contains sanity probes, every mined prefix/core, core size
and action-frequency summaries, and separate BASE/MINED replay stages. The
CSV has one row per probe or master stage. In late-append comparison, MINED
checks that the replay history and pre-append final query match BASE exactly;
it stops before the final solve if they differ. A collection of UNSAT prefix
cores only excludes those prefixes and does not prove the rest of the search
space UNSAT.

### Prefix trie mining

`prefix-trie-mining` builds a trie from deterministic heuristic schedules and
probes prefixes from short to long. An UNSAT prefix prunes its descendants; a
timeout is UNKNOWN, so its descendants are still probed. Before mining, it
rechecks the known incumbent prefixes at depths 3 and 5. The backend must
support `propagate()`; the command checks this interface before starting. A
propagation contradiction is an exact UNSAT result for that prefix.

```bash
python -m ffp_sat.diagnose prefix-trie-mining \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --schedule /tmp/ffp-diagnostic-incumbent.json \
  --pool-size 128 --prefix-depth 5 \
  --depth-budgets 60 20 10 2 1 --seed 0 --jobs 1 \
  --replay-bounds 991 990 --replay-query-time 90 \
  --final-query-time 600 --solver cadical300
```

Each prefix probe uses a fresh worker and CaDiCaL instance. Probe budgets are
selected by depth. UNSAT prefixes become guarded clauses expressed with
semantic `(round, vertex, polarity)` actions, never worker variable IDs. The
master comparison runs BASE and trie-mined replays on separate solvers, checks
that their history through the final query is identical, and appends learned
clauses only after the final query has been built. SAT schedules are checked
with the simulator. The final JSON/CSV are
`diagnostics/prefix_trie_mining.*`.

The report distinguishes `UNSAT_PROPAGATION`, `UNSAT_SOLVE`, `TIMEOUT`, and
`ERROR`. Its depth histogram counts the shortest proved UNSAT trie nodes; it
does not call a prefix minimal unless every shorter ancestor was also proved
UNSAT. Mined prefixes are acceleration data, not a proof that the unprobed
search space is UNSAT.

### Early action exactness

`early-action-exact` first runs a containment-only search to prove the earliest
containment round. It validates a supplied upper schedule when present; without
one, it obtains a SAT witness at the stated upper bound. It then checks lower
horizons with fresh solver processes. A timeout leaves the minimum unknown and
stops the experiment before any action clauses are added.

Before the full instance runs, the command checks BASE/EARLY-EXACT SAT
equivalence against a brute-force oracle on exhaustive graphs up to three
vertices, seeded graphs of four to six vertices, and an explicit idle-final
containment case. For one firefighter, EARLY-EXACT adds one disjunction of
round action variables for each proved early round; it introduces no new
variables. `--exact-range through` includes the minimum-containment round,
while `before` omits that round.

```bash
python -m ffp_sat.diagnose early-action-exact \
  dataset/1000_ep0.0075_0_gilbert_1.in \
  --firefighters 1 --T 9 --K 989 \
  --known-containment-upper 6 \
  --upper-schedule /tmp/ffp-diagnostic-incumbent.json \
  --containment-query-time 120 \
  --replay-bounds 991 990 --replay-query-time 90 \
  --final-query-time 600 --solver cadical300
```

The replay is late-append: BASE and EARLY-EXACT solve the same bounds first,
then the exact-action clauses are appended only to EARLY-EXACT after the final
query has been built. It verifies the pre-append formula and history match,
checks that zero variables and exactly one clause per requested round were
added, and validates any SAT schedule with the simulator. Results are written
to `diagnostics/early_action_exact.json` and `.csv`; a new SAT schedule is also
saved as `diagnostics/early_exact_solution.json`.
