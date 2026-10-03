import time
from contextlib import nullcontext

from pysat.solvers import Solver

from .encoder import Encoder
from .horizon import compute_horizon_bounds, initial_horizon, next_horizon
from .simulator import Solution


def search(
    instance,
    firefighters,
    best,
    lower,
    distance,
    backend,
    deadline,
    publish,
    stats,
    solver_instance=None,
    initial_horizon_factor=1.5,
    horizon_growth_factor=2.0,
    cnf_export_prefix=None,
    debug=False,
):
    upper = best.k
    bounds = compute_horizon_bounds(instance.n, len(instance.initial_fire), firefighters, lower, upper)
    horizon = initial_horizon(best.containment_time, bounds.certification, initial_horizon_factor)
    stats.update(bounds.stats(), current_t=horizon, initial_t=horizon, max_encoded_t=0)
    if lower == upper:
        return best, lower
    with Solver(name=backend) if solver_instance is None else nullcontext(solver_instance) as solver:
        start = time.monotonic()
        encoder = Encoder(
            instance, firefighters, solver, distance,
            capture_cnf=cnf_export_prefix is not None,
            debug=debug,
            capture_clauses=debug,
        )
        stats["encoding_time"] += time.monotonic() - start
        exported_cnf = False
        try:
            while lower < upper and time.monotonic() < deadline:
                start = time.monotonic()
                encoder.ensure_horizon(horizon)
                bound = upper - 1
                assumptions = encoder.assumptions(horizon, bound, upper)
                stats["encoding_time"] += time.monotonic() - start
                if debug:
                    stats.setdefault("debug_profile", {})["encoding"] = encoder.debug_profile()
                exported_now = False
                if cnf_export_prefix is not None and not exported_cnf:
                    export_started = time.monotonic()
                    stats["cnf_export"] = encoder.export_dimacs(
                        cnf_export_prefix, assumptions, horizon, bound
                    )
                    stats["encoding_time"] += time.monotonic() - export_started
                    exported_cnf = True
                    exported_now = True
                stats.update(encoder.stats(), current_t=horizon, max_encoded_t=encoder.horizon)
                if time.monotonic() >= deadline:
                    if exported_now:
                        stats.update(
                            current_k_bound=bound,
                            query_horizon=horizon,
                            query_t_cert=bounds.certification,
                            update_source="CNF_EXPORT",
                        )
                        publish(best, lower, stats)
                    break
                propagation = None
                if debug:
                    probe = None
                    profile_started = time.monotonic()
                    try:
                        # Probe on an isolated solver. Some native backends can crash
                        # when propagate() is mixed repeatedly with solve() on one instance.
                        probe = Solver(name=backend, bootstrap_with=encoder.cnf)
                        probe_setup_time = time.monotonic() - profile_started
                        propagation_started = time.monotonic()
                        consistent, propagated = probe.propagate(assumptions=assumptions)
                        variables = {abs(literal) for literal in propagated}
                        propagation = {
                            "supported": True,
                            "consistent": bool(consistent),
                            "propagated_variables": len(variables),
                            "variables_total": encoder.vars.top,
                            "propagation_ratio": len(variables) / encoder.vars.top if encoder.vars.top else 0.0,
                            "probe_setup_time_seconds": probe_setup_time,
                            "time_seconds": time.monotonic() - propagation_started,
                        }
                    except (NotImplementedError, AttributeError) as exc:
                        propagation = {
                            "supported": False,
                            "reason": f"{type(exc).__name__}: {exc}",
                            "propagated_variables": None,
                            "variables_total": encoder.vars.top,
                            "propagation_ratio": None,
                            "probe_setup_time_seconds": time.monotonic() - profile_started,
                            "time_seconds": 0.0,
                        }
                    except Exception as exc:
                        # Profiling is diagnostic and must not prevent the actual SAT query.
                        propagation = {
                            "supported": False,
                            "reason": f"{type(exc).__name__}: {exc}",
                            "propagated_variables": None,
                            "variables_total": encoder.vars.top,
                            "propagation_ratio": None,
                            "probe_setup_time_seconds": time.monotonic() - profile_started,
                            "time_seconds": 0.0,
                        }
                    finally:
                        if probe is not None:
                            probe.delete()
                    stats["propagation_time"] = stats.get("propagation_time", 0.0) + propagation["time_seconds"]
                    stats["debug_probe_setup_time"] = stats.get("debug_probe_setup_time", 0.0) + propagation[
                        "probe_setup_time_seconds"
                    ]
                    propagation.update(horizon=horizon, k_bound=bound, assumption_count=len(assumptions))
                    stats.setdefault("debug_profile", {}).setdefault("queries", []).append(propagation)
                    if time.monotonic() >= deadline:
                        propagation["solve_status"] = "NOT_RUN_TIME_LIMIT"
                        stats.update(
                            current_k_bound=bound,
                            query_horizon=horizon,
                            query_t_cert=bounds.certification,
                            update_source="DEBUG_PROPAGATION",
                        )
                        publish(best, lower, stats)
                        break
                stats["sat_calls"] += 1
                if debug:
                    propagation["solve_status"] = "RUNNING"
                stats["current_k_bound"] = bound
                stats["query_horizon"] = horizon
                stats["query_t_cert"] = bounds.certification
                stats["update_source"] = "SAT_QUERY"
                publish(best, lower, stats)
                start = time.monotonic()
                sat = solver.solve(assumptions=assumptions)
                stats["sat_time"] += time.monotonic() - start
                if debug:
                    propagation["solve_status"] = "SAT" if sat else "UNSAT"
                    stats["debug_profile"]["encoding"] = encoder.debug_profile()
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
                    bounds = compute_horizon_bounds(
                        instance.n, len(instance.initial_fire), firefighters, lower, upper
                    )
                    stats.update(bounds.stats())
                else:
                    stats["unsat_results"] += 1
                    source = "UNSAT"
                    stats["certifying"] = horizon >= bounds.certification
                    if stats["certifying"]:
                        lower = upper
                        bounds = compute_horizon_bounds(
                            instance.n, len(instance.initial_fire), firefighters, lower, upper
                        )
                        stats.update(bounds.stats())
                    else:
                        horizon = next_horizon(horizon, bounds.certification, horizon_growth_factor)
                if not lower <= upper:
                    raise AssertionError("Invalid objective bounds")
                stats["current_t"] = horizon
                stats["update_source"] = source
                publish(best, lower, stats)
            return best, lower
        finally:
            encoder.close()
