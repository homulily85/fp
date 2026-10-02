import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

from .cli import add_options, configuration
from .result import write_json
from .worker import run


def parse_firefighter_values(values):
    result = []
    for value in values:
        match = re.fullmatch(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]", value)
        if match:
            first, last = map(int, match.groups())
            if first < 1 or last < first:
                raise argparse.ArgumentTypeError("Firefighter range must satisfy 1 <= a <= b")
            result.extend(range(first, last + 1))
        else:
            try:
                number = int(value)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    f"Expected a firefighter count or inclusive range [a,b], got {value!r}"
                ) from exc
            if number < 1:
                raise argparse.ArgumentTypeError("Firefighter counts must be positive")
            result.append(number)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sequential FFP experiments")
    parser.add_argument("input", type=Path, help="One .in instance file or a directory of .in files")
    parser.add_argument("--firefighters", nargs="+", required=True, metavar="D_OR_[A,B]")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    add_options(parser)
    args = parser.parse_args(argv)
    try:
        firefighters = parse_firefighter_values(args.firefighters)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.input.is_file():
        if args.input.suffix != ".in":
            parser.error("Input file must have the .in extension")
        instances = [args.input]
        dataset_name = args.input.stem
    elif args.input.is_dir():
        instances = sorted(args.input.glob("*.in"))
        dataset_name = args.input.resolve().name
    else:
        parser.error("Input must be an existing .in file or dataset directory")
    if not instances:
        parser.error("Input directory contains no .in files")
    if args.out_dir is None:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        args.out_dir = Path("results") / f"{dataset_name}_sat-ffp_{timestamp}"
    jobs = [
        (p, d, args.out_dir / f"{p.stem}_D{d}_seed{args.seed}.json")
        for p in instances
        for d in sorted(set(firefighters))
    ]
    csv_path = args.out_dir / "summary.csv"
    if not args.overwrite and (csv_path.exists() or any(out.exists() for _, _, out in jobs)):
        parser.error("Output exists; use --overwrite to replace it")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "instance",
        "firefighters",
        "status",
        "termination",
        "best_k",
        "saved",
        "lower_bound",
        "upper_bound",
        "gap_abs",
        "gap_rel",
        "elapsed_total",
        "final_validation_time",
        "final_validation",
        "sat_calls",
        "sat_results",
        "unsat_results",
        "n_semantic_vars",
        "n_aux_vars",
        "n_clauses",
        "encoding_time",
        "sat_time",
        "error",
    ]
    failed = False
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for path, d, output in jobs:
            result = run(path, d, configuration(args))
            write_json(output, result)
            writer.writerow(result)
            stream.flush()
            print(f"{path.name} D={d}: {result['status']} K={result.get('best_k')}", flush=True)
            failed |= result["status"] == "ERROR"
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
