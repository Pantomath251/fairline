"""Independent-Poisson goal model fitted to de-vigged 1X2 probabilities.

Bookmakers price the match result; prediction markets ask richer questions ("win by 2+", "over 2.5 goals",
"concede in the first 25 minutes", "exact score 3-2"). Fitting expected goals (lambda_home, lambda_away) to the
fair home/away win probabilities lets Fairline price all of them consistently."""
from __future__ import annotations

import math
from functools import lru_cache

MAX_GOALS = 12


def _pmf(lam: float) -> list[float]:
    out, p = [], math.exp(-lam)
    for k in range(MAX_GOALS + 1):
        out.append(p)
        p = p * lam / (k + 1)
    return out


class GoalModel:
    def __init__(self, lam_home: float, lam_away: float):
        self.lh, self.la = lam_home, lam_away
        ph, pa = _pmf(lam_home), _pmf(lam_away)
        self.grid = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]

    def prob(self, pred) -> float:
        return sum(self.grid[i][j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1) if pred(i, j))

    def result(self) -> dict:
        return {"home": self.prob(lambda i, j: i > j), "draw": self.prob(lambda i, j: i == j),
                "away": self.prob(lambda i, j: i < j)}

    def margin(self, side: str, at_least: int) -> float:
        return self.prob((lambda i, j: i - j >= at_least) if side == "home" else (lambda i, j: j - i >= at_least))

    def total_over(self, line: float) -> float:
        return self.prob(lambda i, j: i + j > line)

    def btts(self) -> float:
        return self.prob(lambda i, j: i > 0 and j > 0)

    def exact(self, h: int, a: int) -> float:
        return self.grid[h][a] if h <= MAX_GOALS and a <= MAX_GOALS else 0.0

    def team_scores(self, side: str, minutes: float | None = None, match_minutes: float = 90.0) -> float:
        lam = self.lh if side == "home" else self.la
        if minutes is not None:
            lam *= max(0.0, min(minutes, match_minutes)) / match_minutes
        return 1.0 - math.exp(-lam)

    def team_total_over(self, side: str, line: float) -> float:
        return self.prob((lambda i, j: i > line) if side == "home" else (lambda i, j: j > line))

    def clean_sheet(self, side: str) -> float:
        return self.prob((lambda i, j: j == 0) if side == "home" else (lambda i, j: i == 0))

    def as_dict(self) -> dict:
        r = self.result()
        return {"lambdaHome": round(self.lh, 3), "lambdaAway": round(self.la, 3),
                "home": round(r["home"], 4), "draw": round(r["draw"], 4), "away": round(r["away"], 4),
                "over25": round(self.total_over(2.5), 4), "btts": round(self.btts(), 4)}


@lru_cache(maxsize=4096)
def fit(p_home: float, p_away: float, total_line: float | None = None, p_over: float | None = None) -> GoalModel:
    """Find lambdas whose implied home/away win probabilities (and, when given, the probability of going over the
    bookmaker's goal-total line) match the targets. Least squares, coarse-to-fine grid."""
    best = (1e9, 1.3, 1.1)

    def err(lh, la):
        gm = GoalModel(lh, la)
        g = gm.result()
        e = (g["home"] - p_home) ** 2 + (g["away"] - p_away) ** 2
        if total_line is not None and p_over is not None:
            e += (gm.total_over(total_line) - p_over) ** 2
        return e

    lo_h, hi_h, lo_a, hi_a = 0.05, 4.5, 0.05, 4.5
    for _ in range(5):
        step_h, step_a = (hi_h - lo_h) / 12, (hi_a - lo_a) / 12
        for a in range(13):
            for b in range(13):
                lh, la = lo_h + a * step_h, lo_a + b * step_a
                e = err(lh, la)
                if e < best[0]:
                    best = (e, lh, la)
        _, ch, ca = best
        lo_h, hi_h = max(0.02, ch - step_h), ch + step_h
        lo_a, hi_a = max(0.02, ca - step_a), ca + step_a
    return GoalModel(best[1], best[2])
