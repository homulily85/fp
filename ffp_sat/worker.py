import importlib.metadata
import sys
import time
from datetime import datetime, timezone

from pysat.solvers import Solver

from .heuristic import portfolio, threat
from .instance import read_instance
from .preprocess import preprocess
from .result import make_result
from .simulator import simulate
from .stk_solver import search


def worker(connection, path, firefighters, config, started, deadline):
    stats = dict(
        read_time=0.0,
        preprocess_time=0.0,
        heuristic_time=0.0,
        encoding_time=0.0,
        sat_time=0.0,
        sat_calls=0,
        sat_results=0,
        unsat_results=0,
        n_semantic_vars=0,
        n_activation_vars=0,
        n_aux_vars=0,
        n_clauses=0,
        number_of_horizon_extensions=0,
        number_of_incumbent_improvements=0,
    )
    latest = None
    solver = None
    try:
        solver = Solver(name=config["solver"])
        config = dict(
            config,
            versions={name: importlib.metadata.version(name) for name in ("python-sat", "networkx", "ruff")},
        )
        start = time.monotonic()
        instance = read_instance(path)
        stats["read_time"] = time.monotonic() - start
        stats["t_max"] = (instance.n + firefighters - 1) // firefighters
        # Send a cheap verified incumbent before portfolio or SAT construction.
        start = time.monotonic()
        heuristic_deadline = min(deadline, start + config["heuristic_budget"])
        best = threat(instance, firefighters)
        stats["heuristic_time"] += time.monotonic() - start
        lower = len(instance.initial_fire)

        def publish(solution, lb, metrics, source=None):
            nonlocal latest
            metrics = dict(
                metrics,
                update_source=source or metrics.get("update_source", "HEURISTIC"),
                snapshot_elapsed=time.monotonic() - started,
            )
            latest = make_result(instance, firefighters, solution, lb, metrics, config)
            connection.send(("CHECKPOINT", latest))

        stats["current_t"] = min(best.containment_time, stats["t_max"])
        publish(best, lower, stats)
        start = time.monotonic()
        distance, lower = preprocess(instance, firefighters)
        stats["preprocess_time"] = time.monotonic() - start
        publish(best, lower, stats, source="PREPROCESS")
        if lower < best.k and time.monotonic() < deadline:
            start = time.monotonic()

            def improved(solution):
                stats["number_of_incumbent_improvements"] += 1
                stats["heuristic_time"] = initial_time + time.monotonic() - start
                publish(solution, lower, stats)

            initial_time = stats["heuristic_time"]
            best, frontier = portfolio(
                instance, firefighters, best, heuristic_deadline, config["seed"], improved
            )
            stats["heuristic_time"] = initial_time + time.monotonic() - start
            stats["pareto_frontier"] = [
                dict(t=s.containment_time, k=s.k, schedule=[list(p) for p in s.schedule]) for s in frontier
            ]
            publish(best, lower, stats)
        if lower < best.k and time.monotonic() < deadline:
            best, lower = search(
                instance,
                firefighters,
                best,
                lower,
                distance,
                config["solver"],
                deadline,
                publish,
                stats,
                solver_instance=solver,
            )
        result = make_result(instance, firefighters, best, lower, stats, config)
        connection.send(("FINAL", result))
    except Exception as exc:
        result = dict(
            latest or {}, status="ERROR", termination="WORKER_ERROR", error=f"{type(exc).__name__}: {exc}"
        )
        connection.send(("ERROR", result))
    finally:
        if solver is not None:
            solver.delete()
        connection.close()


def validate_final_result(path, firefighters, result):
    """Check the returned incumbent once, after solving, outside solver timing."""
    schedule = result.get("schedule")
    if schedule is None:
        result["final_validation"] = "SKIPPED_NO_SCHEDULE"
        result["final_validation_time"] = 0.0
        return result

    started = time.monotonic()
    try:
        instance = read_instance(path)
        solution = simulate(instance, firefighters, schedule)
        if solution.k != result.get("best_k") or solution.k != result.get("upper_bound"):
            raise AssertionError(
                f"Simulator K={solution.k} disagrees with reported incumbent "
                f"K={result.get('best_k')} / UB={result.get('upper_bound')}"
            )
        if result.get("lower_bound") is not None and result["lower_bound"] > solution.k:
            raise AssertionError("Reported lower bound exceeds verified incumbent")
        result.update(
            schedule=[list(actions) for actions in solution.schedule],
            best_k=solution.k,
            upper_bound=solution.k,
            saved=instance.n - solution.k,
            best_containment_time=solution.containment_time,
            gap_abs=solution.k - result["lower_bound"] if result.get("lower_bound") is not None else None,
            gap_rel=(solution.k - result["lower_bound"]) / solution.k
            if result.get("lower_bound") is not None and solution.k
            else 0.0,
        )
        if result.get("lower_bound") == solution.k:
            result.update(status="OPTIMAL", termination="PROVEN")
        result["final_validation"] = "PASSED"
    except Exception as exc:
        result.update(
            status="ERROR",
            termination="FINAL_VALIDATION_ERROR",
            final_validation="FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        result["final_validation_time"] = time.monotonic() - started
    return result


def run(path, firefighters, config, target=worker):
    import multiprocessing
    from pathlib import Path

    started = time.monotonic()
    deadline = started + config["time_limit"]
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=target, args=(sender, str(path), firefighters, config, started, deadline)
    )
    latest = final = None
    timed_out = False
    logged_state = None
    process.start()
    sender.close()
    try:
        while final is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if receiver.poll(min(0.1, remaining)):
                try:
                    kind, message = receiver.recv()
                except (EOFError, OSError):
                    break
                if kind == "CHECKPOINT":
                    latest = message
                    state = tuple(
                        latest.get(key)
                        for key in (
                            "update_source",
                            "lower_bound",
                            "upper_bound",
                            "current_t",
                            "current_k_bound",
                            "sat_calls",
                            "sat_results",
                            "unsat_results",
                        )
                    )
                    if state != logged_state:
                        logged_state = state
                        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        source = latest.get("update_source", "STATUS")
                        details = (
                            f" query=K<={latest['current_k_bound']}"
                            if latest.get("current_k_bound") is not None
                            else ""
                        )
                        print(
                            f"[{timestamp}] {Path(path).name} [{source}]: "
                            f"LB={latest.get('lower_bound')} UB={latest.get('upper_bound')} "
                            f"T={latest.get('current_t')}{details} "
                            f"SAT calls={latest.get('sat_calls', 0)} "
                            f"(SAT={latest.get('sat_results', 0)}, UNSAT={latest.get('unsat_results', 0)}) "
                            f"elapsed={time.monotonic() - started:.1f}s",
                            file=sys.stderr,
                            flush=True,
                        )
                else:
                    final = message
            elif not process.is_alive():
                break
        if process.is_alive() and final is None:
            process.terminate()
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join(0.2)
        while receiver.poll():
            try:
                kind, message = receiver.recv()
            except (EOFError, OSError):
                break
            if kind == "CHECKPOINT":
                latest = message
            else:
                final = message
        if final is not None:
            result = final
        elif timed_out and latest is not None:
            result = dict(
                latest,
                status="OPTIMAL" if latest["lower_bound"] == latest["upper_bound"] else "FEASIBLE",
                termination="PROVEN" if latest["lower_bound"] == latest["upper_bound"] else "TIME_LIMIT",
            )
        else:
            result = dict(
                latest or {},
                status="ERROR",
                termination="TIME_LIMIT_NO_INCUMBENT" if timed_out else "WORKER_EXIT",
                error="No verified incumbent before deadline"
                if timed_out
                else f"Worker exited with code {process.exitcode}",
            )
            if latest is None:
                result.update(
                    schedule=None, upper_bound=None, lower_bound=None, best_k=None, gap_abs=None, gap_rel=None
                )
        # Freeze solver wall time before the final trusted-check pass.
        result["elapsed_total"] = time.monotonic() - started
        result["total_time"] = result["elapsed_total"]
        for field in (
            "n",
            "m",
            "best_k",
            "saved",
            "lower_bound",
            "upper_bound",
            "gap_abs",
            "gap_rel",
            "schedule",
            "best_containment_time",
            "t_max",
        ):
            result.setdefault(field, None)
        result.setdefault("instance", str(path))
        result.setdefault("firefighters", firefighters)
        result.setdefault("config", config)
        return validate_final_result(path, firefighters, result)
    finally:
        if process.is_alive():
            process.kill()
            process.join(0.2)
        receiver.close()
        if not process.is_alive():
            process.close()
