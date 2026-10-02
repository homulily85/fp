"""Safe horizons for a canonical optimal firefighter strategy.

Before its final round, a canonical optimum defends D vertices and burns at
least one new vertex each round. The defended vertices and final burned set
are disjoint. These properties give the structural and LB bounds below.
Before the final round one untouched vertex remains, hence
|B| + (D+1)(T-1) <= n-1 and T <= ceil((n-|B|)/(D+1)).
Also L + D(T-1) <= n gives T <= floor((n-L)/D)+1.
For a strictly better incumbent, K <= U-1 also bounds the number of spreading
rounds: |B| + T-1 <= K <= U-1 gives T <= U-|B|.
All bounds refer to the same canonical optimum.
"""

from dataclasses import dataclass
from math import ceil, isfinite


@dataclass(frozen=True)
class HorizonBounds:
    old_safe: int
    structural: int
    incumbent: int
    lower_bound: int
    certification: int

    def stats(self):
        return dict(
            t_max=self.old_safe,
            t_old_safe=self.old_safe,
            t_struct=self.structural,
            t_from_ub=self.incumbent,
            t_from_lb=self.lower_bound,
            t_cert=self.certification,
        )


def ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def compute_horizon_bounds(
    n: int,
    initial_burned: int,
    firefighters: int,
    lower_bound: int,
    upper_bound: int,
) -> HorizonBounds:
    if firefighters < 1 or not 1 <= initial_burned <= lower_bound <= upper_bound <= n:
        raise ValueError("Invalid horizon-bound inputs")
    old = ceil_div(n, firefighters)
    structural = ceil_div(n - initial_burned, firefighters + 1)
    incumbent = upper_bound - initial_burned
    lower = (n - lower_bound) // firefighters + 1
    certification = min(structural, incumbent, lower)
    assert 0 <= certification <= old
    return HorizonBounds(old, structural, incumbent, lower, certification)


def _scaled_horizon(t, cap, factor):
    if not isfinite(factor) or factor < 1:
        raise ValueError("Horizon factors must be finite and at least 1")
    # Clip before multiplying to avoid overflow for large user-supplied factors.
    if t == 0:
        return 0
    if factor >= cap / t:
        return cap
    return min(ceil(factor * t), cap)


def initial_horizon(containment_time, maximum, factor=1.5):
    return _scaled_horizon(containment_time, maximum, factor)


def next_horizon(current, maximum, factor=2.0):
    return min(maximum, max(current + 1, _scaled_horizon(current, maximum, factor)))
