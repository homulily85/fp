import argparse
import math
import sys

from .result import write_json
from .worker import run


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return number


def horizon_factor(value):
    number = positive_float(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Horizon factor must be at least 1")
    return number


def add_options(parser):
    parser.add_argument("--time-limit", type=positive_float, default=600.0)
    parser.add_argument("--solver", default="cadical300")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--heuristic-budget", type=positive_float)
    parser.add_argument("--initial-horizon-factor", type=horizon_factor, default=1.5)
    parser.add_argument("--horizon-growth-factor", type=horizon_factor, default=2.0)


def configuration(args):
    return dict(
        time_limit=args.time_limit,
        solver=args.solver,
        seed=args.seed,
        heuristic_budget=args.heuristic_budget
        if args.heuristic_budget is not None
        else min(5.0, 0.05 * args.time_limit),
        initial_horizon_factor=args.initial_horizon_factor,
        horizon_growth_factor=args.horizon_growth_factor,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exact incremental FFP SAT solver")
    parser.add_argument("instance")
    parser.add_argument("--firefighters", type=positive_int, required=True)
    parser.add_argument("--json-out")
    add_options(parser)
    args = parser.parse_args(argv)
    result = run(args.instance, args.firefighters, configuration(args))
    if args.json_out:
        write_json(args.json_out, result)
    print(
        f"{result['status']} {result['termination']} K={result.get('best_k')} "
        f"LB={result.get('lower_bound')} UB={result.get('upper_bound')} elapsed={result['elapsed_total']:.3f}s"
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    return 1 if result["status"] == "ERROR" else 0
