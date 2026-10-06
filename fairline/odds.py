"""Bookmaker odds -> fair probabilities, and fair probability vs. prediction-market price -> edge."""
from __future__ import annotations

import math


def implied(decimal_odds: list[float | None]) -> list[float] | None:
    if not decimal_odds or any(o is None or o <= 1.0 for o in decimal_odds):
        return None
    return [1.0 / o for o in decimal_odds]


def overround(decimal_odds: list[float | None]) -> float | None:
    imp = implied(decimal_odds)
    return round(sum(imp) - 1.0, 4) if imp else None


def devig_proportional(decimal_odds: list[float | None]) -> list[float] | None:
    imp = implied(decimal_odds)
    if not imp:
        return None
    s = sum(imp)
    return [p / s for p in imp]


def devig_power(decimal_odds: list[float | None], tol: float = 1e-10) -> list[float] | None:
    """Power method: find k with sum(p_i ** k) == 1. Corrects favourite-longshot bias better than proportional
    normalisation (longshots carry more of the bookmaker margin)."""
    imp = implied(decimal_odds)
    if not imp:
        return None
    if abs(sum(imp) - 1.0) < tol:
        return imp
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(p ** k for p in imp)
        if s > 1.0:
            lo = k
        else:
            hi = k
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    out = [p ** k for p in imp]
    s = sum(out)
    return [p / s for p in out]


def fair_from_fixture(fx: dict) -> dict | None:
    """{'home':p,'draw':p|None,'away':p} from a fixture carrying oddsHome/oddsDraw/oddsAway (decimal)."""
    h, d, a = fx.get("oddsHome"), fx.get("oddsDraw"), fx.get("oddsAway")
    if d:
        p = devig_power([h, d, a])
        if not p:
            return None
        if fx.get("sport") not in (None, "football"):
            # 3-way regulation odds (e.g. hockey from the fallback source): markets settle including OT/shootout,
            # so split the regulation draw between the sides in proportion to their win chances.
            share = p[0] / (p[0] + p[2])
            return {"home": p[0] + p[1] * share, "draw": None, "away": p[2] + p[1] * (1 - share),
                    "overround": overround([h, d, a])}
        return {"home": p[0], "draw": p[1], "away": p[2], "overround": overround([h, d, a])}
    p = devig_power([h, a])
    return {"home": p[0], "draw": None, "away": p[1], "overround": overround([h, a])} if p else None


def edge(fair_yes: float, yes_price: float, fee_bps: float = 200.0) -> dict:
    """Compare a fair YES probability with the market's YES price (USDC per share, pays 1 USDC if YES).

    Returns the edge for the better side: buying YES at yes_price or NO at (1 - yes_price) when no separate NO
    price is given. ev is expected profit per 1 USDC staked after the primary fee; kelly is the full-Kelly stake
    fraction (the UI shows a quarter of it)."""
    fee = fee_bps / 10_000.0
    q_yes = min(max(yes_price, 1e-6), 1 - 1e-6)
    out = {}
    for side, p, q in (("yes", fair_yes, q_yes), ("no", 1 - fair_yes, 1 - q_yes)):
        ev = p / q * (1 - fee) - 1.0
        kelly = max(0.0, (p * (1 - fee) - q) / ((1 - fee) - q)) if (1 - fee) > q else 0.0
        out[side] = {"fair": round(p, 4), "price": round(q, 4), "edge": round(p - q, 4), "ev": round(ev, 4),
                     "kelly": round(kelly, 4)}
    best = max(out.values(), key=lambda x: x["ev"])
    best_side = "yes" if best is out["yes"] else "no"
    return {"bestSide": best_side, "value": best["ev"] > 0, **out}


def to_decimal(p: float) -> float | None:
    return round(1.0 / p, 3) if p and p > 0 else None


def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))
