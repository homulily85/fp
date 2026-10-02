import time
from contextlib import nullcontext

from pysat.solvers import Solver

from .encoder import Encoder
from .simulator import Solution


def search(
    instance, firefighters, best, lower, distance, backend, deadline, publish, stats, solver_instance=None
):
    upper = best.k
    maximum = (instance.n + firefighters - 1) // firefighters
    horizon = min(best.containment_time, maximum)
    stats.update(current_t=horizon, t_max=maximum)
    with Solver(name=backend) if solver_instance is None else nullcontext(solver_instance) as solver:
        start = time.monotonic()
        encoder = Encoder(instance, firefighters, solver, distance)
        stats["encoding_time"] += time.monotonic() - start
        try:
            while lower < upper and time.monotonic() < deadline:
                start = time.monotonic()
                encoder.ensure_horizon(horizon)
                bound = upper - 1
                assumptions = encoder.assumptions(horizon, bound, upper)
                stats["encoding_time"] += time.monotonic() - start
                stats.update(encoder.stats(), current_t=horizon)
                if time.monotonic() >= deadline:
                    break
                stats["sat_calls"] += 1
                stats["current_k_bound"] = bound
                stats["update_source"] = "SAT_QUERY"
                publish(best, lower, stats)
                start = time.monotonic()
                sat = solver.solve(assumptions=assumptions)
                stats["sat_time"] += time.monotonic() - start
                if sat:
                    stats["sat_results"] += 1
                    source = "SAT"
                    model = set(solver.get_model())
                    encoded_burned = frozenset(v for v in range(instance.n) if encoder.b[v, horizon] in model)
                    encoded_defended = frozenset(
                        v for v in range(instance.n) if encoder.d[v, horizon] in model
                    )
                    candidate = Solution(
                        tuple(encoder.decode(model, horizon)),
                        encoded_burned,
                        encoded_defended,
                        horizon,
                    )
                    if candidate.k >= upper or candidate.k > bound:
                        raise AssertionError("SAT model does not improve the incumbent bound")
                    best, upper = candidate, candidate.k
                    stats["number_of_incumbent_improvements"] += 1
                else:
                    stats["unsat_results"] += 1
                    source = "UNSAT"
                    if horizon == maximum:
                        lower = upper
                    else:
                        horizon = min(2 * horizon, maximum)
                if not lower <= upper:
                    raise AssertionError("Invalid objective bounds")
                stats["current_t"] = horizon
                stats["update_source"] = source
                publish(best, lower, stats)
            return best, lower
        finally:
            encoder.close()
