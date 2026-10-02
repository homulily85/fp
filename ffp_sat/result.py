import json
import os
import tempfile
from pathlib import Path


def make_result(instance, firefighters, best, lower, stats, config):
    upper = best.k
    return dict(
        instance=instance.name,
        n=instance.n,
        m=instance.m,
        firefighters=firefighters,
        status="OPTIMAL" if lower == upper else "FEASIBLE",
        termination="PROVEN" if lower == upper else "TIME_LIMIT",
        best_k=upper,
        saved=instance.n - upper,
        lower_bound=lower,
        upper_bound=upper,
        gap_abs=upper - lower,
        gap_rel=(upper - lower) / upper if upper else 0.0,
        best_containment_time=best.containment_time,
        schedule=[list(p) for p in best.schedule],
        metadata=dict(
            instance_seed=instance.seed,
            b_description=instance.description,
            initial_fire=sorted(instance.initial_fire),
        ),
        config=config,
        **stats,
    )


def write_json(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
