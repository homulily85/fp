import json
import os
import tempfile
from pathlib import Path


def make_result(instance, firefighters, best, lower, stats, config):
    upper = best.k
    cnf_export = stats.get("cnf_export")
    debug_profile = stats.get("debug_profile")
    result = dict(
        instance=instance.name,
        n=instance.n,
        m=instance.m,
        firefighters=firefighters,
        initial_horizon_factor=config.get("initial_horizon_factor", 1.5),
        horizon_growth_factor=config.get("horizon_growth_factor", 2.0),
        status="OPTIMAL" if lower == upper else "FEASIBLE",
        termination="PROVEN" if lower == upper else "TIME_LIMIT",
        best_k=upper,
        saved=instance.n - upper,
        lower_bound=lower,
        upper_bound=upper,
        gap_abs=upper - lower,
        gap_rel=(upper - lower) / upper if upper else 0.0,
        best_containment_time=best.containment_time,
        incumbent_horizon=best.containment_time,
        containment_semantics="stable_state_after_round",
        reason=None,
        cnf_export_raw=cnf_export.get("raw") if cnf_export else None,
        cnf_export_named=cnf_export.get("named") if cnf_export else None,
        schedule=[list(p) for p in best.schedule],
        metadata=dict(
            instance_seed=instance.seed,
            b_description=instance.description,
            initial_fire=sorted(instance.initial_fire),
        ),
        config=config,
        **{key: value for key, value in stats.items() if key != "debug_profile"},
    )
    if config.get("debug") and debug_profile is not None:
        result["debug"] = debug_profile
    return result


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
