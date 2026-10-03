from math import ceil
from statistics import fmean, median


def degree_profile(adjacency):
    """Summarize degrees using the nearest-rank percentile convention."""
    values = sorted(map(len, adjacency))
    if not values:
        return {key: 0 for key in ("min", "median", "mean", "p90", "p95", "p99", "max")}

    def nearest_rank(percentile):
        return values[max(0, ceil(percentile * len(values)) - 1)]

    return {
        "min": values[0],
        "median": median(values),
        "mean": fmean(values),
        "p90": nearest_rank(0.90),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
        "max": values[-1],
        "percentile_method": "nearest_rank",
    }
