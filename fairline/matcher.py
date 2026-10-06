"""Read a prediction-market question, find the real fixture it is about, and price it.

Handles the question shapes people actually write on Panta: "Will A beat B", "A will win against B with 2 or more
goals", "Will A beat B 3-2", "A will concede in the first 25 minutes against B", draws, totals, both-teams-to-score,
clean sheets and "A not to lose". Football questions are priced with the Poisson goal model; other sports use the
de-vigged moneyline."""
from __future__ import annotations

import re
import unicodedata

from . import goals

STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "rc", "cd", "ud", "sd", "club", "de", "the", "calcio", "sk", "fk",
        "bk", "if", "cp", "sv", "vfb", "vfl", "tsg", "1", "04", "05", "09", "1899", "1846", "1907", "1910", "1913"}
GENERIC = {"united", "city", "town", "real", "sporting", "athletic", "atletico", "inter", "national", "women",
           "olympique", "racing", "dynamo", "dinamo", "red", "star", "young", "boys", "rovers", "wanderers", "county",
           "saint", "st", "san", "santa", "union", "deportivo", "independiente", "nacional", "central", "hotspur", "albion"}
NICKNAMES = {
    "tottenham hotspur": ["spurs", "tottenham"], "manchester united": ["man utd", "man united", "manchester utd"],
    "manchester city": ["man city"], "paris saint-germain": ["psg", "paris sg", "paris"],
    "fc barcelona": ["barca", "barcelona"], "inter": ["inter milan", "internazionale"],
    "juventus": ["juve"], "fc bayern munchen": ["bayern", "bayern munich"], "bayern munchen": ["bayern", "bayern munich"],
    "atletico madrid": ["atleti", "atletico"], "wolverhampton": ["wolves"], "brighton & hove albion": ["brighton"],
    "newcastle united": ["newcastle"], "west ham united": ["west ham"], "nottingham forest": ["forest", "nottm forest"],
    "borussia dortmund": ["dortmund", "bvb"], "bayer 04 leverkusen": ["leverkusen"], "ac milan": ["milan"],
    "sl benfica": ["benfica"], "fc porto": ["porto"], "sporting cp": ["sporting", "sporting lisbon"],
    "afc ajax": ["ajax"], "psv eindhoven": ["psv"], "united states": ["usa", "usmnt", "us"],
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^a-z0-9.:\-+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def aliases(name: str, short: str | None = None) -> list[str]:
    n = norm(name)
    out = {n}
    if short:
        out.add(norm(short))
    core = " ".join(t for t in n.split() if t not in STOP)
    if core:
        out.add(core)
    for k, v in NICKNAMES.items():
        if norm(k) in (n, core):
            out.update(norm(x) for x in v)
    toks = [t for t in core.split() if t not in GENERIC and len(t) >= 4]
    if len(toks) == 1:
        out.add(toks[0])  # "Arsenal", "Chelsea", "Liverpool"
    elif toks and len(core.split()) >= 2 and len(toks[0]) >= 5:
        out.add(toks[0])  # "Tottenham Hotspur" -> "tottenham"; "Real Madrid" keeps "real madrid" (real is generic)
    out.discard("")
    out = {re.sub(r"\s*\(.*?\)\s*", " ", a).strip() for a in out}
    return sorted((a for a in out if len(a) >= 3), key=len, reverse=True)


def _find(text: str, names: list[str]) -> tuple[int, int, str] | None:
    best = None
    for a in names:
        m = re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", text)
        if m and (best is None or len(a) > len(best[2])):
            best = (m.start(), m.end(), a)
    return best


def locate(question: str, fx: dict) -> dict | None:
    """Template the question with @H / @A placeholders when both teams of the fixture are mentioned."""
    q = norm(re.sub(r"\(.*?\)", " ", question))
    h = _find(q, aliases(fx["home"], fx.get("homeShort")))
    a = _find(q, aliases(fx["away"], fx.get("awayShort")))
    if not h or not a or (h[0] < a[1] and a[0] < h[1]):
        return None
    spans = sorted([(h[0], h[1], "@H"), (a[0], a[1], "@A")])
    t = q
    for s, e, tok in reversed(spans):
        t = t[:s] + tok + t[e:]
    return {"text": t, "score": len(h[2]) + len(a[2])}


WIN = r"(?:beat|beats|defeat|defeats|win against|wins against|win vs|win over|win|wins|overcome|edge)"


def _subject(t: str) -> str | None:
    m = re.search(r"\bwill (@H|@A)\b", t) or re.search(r"^(?:will )?(@H|@A)\b", t) or re.search(r"(@H|@A)", t)
    return m.group(1) if m else None


def parse(t: str) -> dict | None:
    """Classify a templated question. Returns {'kind', 'side', ...} with side in {'home','away'} where relevant."""
    side_of = {"@H": "home", "@A": "away"}
    other = {"home": "away", "away": "home"}
    subj = _subject(t)
    s = side_of.get(subj or "", "home")

    if re.search(r"both teams (?:to |will )?score|btts", t):
        return {"kind": "btts", "negate": bool(re.search(r"\bnot\b|\bfail\b", t))}

    m = re.search(r"(@H|@A)[^0-9]*?\b(\d{1,2})\s*[-:]\s*(\d{1,2})\b", t)
    if m and re.search(WIN + r"|\bscore", t):
        team, x, y = side_of[m.group(1)], int(m.group(2)), int(m.group(3))
        if re.search(r"(@H|@A)\s+(\d{1,2})\s*[-:]\s*(\d{1,2})\s+(@H|@A)", t):  # "@H 2-1 @A" reads left to right
            return {"kind": "exact", "home": x if t.index("@H") < t.index("@A") else y,
                    "away": y if t.index("@H") < t.index("@A") else x}
        return {"kind": "exact", "home": x if team == "home" else y, "away": y if team == "home" else x}

    m = re.search(r"concede[sd]?\b.*?first (\d{1,3}) min", t) or re.search(r"concede[sd]? (?:a goal )?(?:before|within) (?:the )?(\d{1,3})(?:th)? min", t)
    if m:
        return {"kind": "team_scores", "side": other[s], "minutes": int(m.group(1))}
    m = re.search(r"score[sd]?\b.*?first (\d{1,3}) min", t) or re.search(r"score[sd]? (?:a goal )?(?:before|within) (?:the )?(\d{1,3})(?:th)? min", t)
    if m:
        return {"kind": "team_scores", "side": s, "minutes": int(m.group(1))}

    if re.search(r"\bdraw\b|\bdraws\b|\btie\b|end(?:s)? (?:in a )?(?:level|tie|draw)", t):
        return {"kind": "draw"}

    if re.search(r"clean sheet|keep (?:a )?clean|not concede|fail to score", t):
        if "fail to score" in t:
            return {"kind": "team_scores", "side": s, "negate": True}
        return {"kind": "clean_sheet", "side": s}

    m = re.search(r"(over|more than|at least|under|fewer than|less than)\s*(\d+(?:\.\d+)?)\s*(?:total\s*)?goals", t)
    if m:
        op, n = m.group(1), float(m.group(2))
        line = n - 0.5 if op == "at least" else n if op in ("over", "more than") else n - 0.5 if op in ("fewer than", "less than") and n == int(n) else n
        under = op in ("under", "fewer than", "less than")
        team_total = re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?score", t)
        if team_total:
            return {"kind": "team_total", "side": side_of[team_total.group(1)], "line": line, "negate": under}
        return {"kind": "total", "line": line, "negate": under}

    if re.search(r"not lose|avoid defeat|unbeaten|draw or win|win or draw", t):
        return {"kind": "not_lose", "side": s}

    m = re.search(r"by (\d+) or more|by at least (\d+)|with (\d+) or more goals|by (\d+)\+|(\d+)\+ goal", t)
    if m and re.search(WIN, t):
        k = int(next(g for g in m.groups() if g))
        return {"kind": "margin", "side": s, "atLeast": k}

    if re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?(?:" + WIN + r")\b", t) or re.search(r"will (@H|@A) " + WIN, t):
        return {"kind": "win", "side": s, "negate": bool(re.search(r"\bnot\b|\bfail to win\b", t))}
    if re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?(?:lose|lose to|lose against)\b", t):
        return {"kind": "win", "side": other[s]}
    if re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?score\b", t):
        return {"kind": "team_scores", "side": s}
    return None


def price(prop: dict, fx: dict) -> float | None:
    """Fair YES probability for a parsed proposition on a fixture."""
    fair = fx["fair"]
    kind = prop["kind"]
    if fx["sport"] != "football" or not fx.get("model"):
        if kind == "win":
            p = fair.get(prop["side"])
            return (1 - p if prop.get("negate") else p) if p is not None else None
        return None
    gm = goals.fit(*fx["modelFit"]) if fx.get("modelFit") else goals.fit(round(fair["home"], 4), round(fair["away"], 4))
    if kind == "win":
        p = fair[prop["side"]]
        return 1 - p if prop.get("negate") else p
    if kind == "draw":
        return fair["draw"]
    if kind == "not_lose":
        return fair[prop["side"]] + fair["draw"]
    if kind == "margin":
        return gm.margin(prop["side"], prop["atLeast"])
    if kind == "exact":
        return gm.exact(prop["home"], prop["away"])
    if kind == "total":
        p = gm.total_over(prop["line"])
        return 1 - p if prop.get("negate") else p
    if kind == "team_total":
        p = gm.team_total_over(prop["side"], prop["line"])
        return 1 - p if prop.get("negate") else p
    if kind == "btts":
        p = gm.btts()
        return 1 - p if prop.get("negate") else p
    if kind == "team_scores":
        p = gm.team_scores(prop["side"], prop.get("minutes"))
        return 1 - p if prop.get("negate") else p
    if kind == "clean_sheet":
        return gm.clean_sheet(prop["side"])
    return None


def describe(prop: dict, fx: dict) -> str:
    team = {"home": fx["home"], "away": fx["away"]}
    k = prop["kind"]
    if k == "win":
        return f"{team[prop['side']]} {'do not ' if prop.get('negate') else ''}win"
    if k == "draw":
        return "Match ends in a draw"
    if k == "not_lose":
        return f"{team[prop['side']]} win or draw"
    if k == "margin":
        return f"{team[prop['side']]} win by {prop['atLeast']}+"
    if k == "exact":
        return f"Exact score {fx['home']} {prop['home']}-{prop['away']} {fx['away']}"
    if k == "total":
        return f"Total goals {'under' if prop.get('negate') else 'over'} {prop['line']}"
    if k == "team_total":
        return f"{team[prop['side']]} goals {'under' if prop.get('negate') else 'over'} {prop['line']}"
    if k == "btts":
        return "Both teams score" + (" — NO" if prop.get("negate") else "")
    if k == "team_scores":
        when = f" in the first {prop['minutes']} min" if prop.get("minutes") else ""
        return f"{team[prop['side']]} {'fail to score' if prop.get('negate') else 'score'}{when}"
    if k == "clean_sheet":
        return f"{team[prop['side']]} keep a clean sheet"
    return k


def match_market(market: dict, fixtures: list[dict]) -> dict | None:
    """Best fixture + priced proposition for a Panta market, or None if it is not about a priced fixture."""
    text = " ".join(x for x in (market.get("title"), market.get("question"), market.get("description")) if x)
    if not text:
        return None
    start = market.get("startTime") or 0
    end = market.get("endTime") or start
    best = None
    for fx in fixtures:
        ko = fx["startTimestamp"]
        if start and not (start - 36 * 3600 <= ko <= max(end, start) + 12 * 3600):
            continue
        loc = locate(text, fx)
        if not loc:
            continue
        prop = parse(loc["text"])
        if not prop:
            continue
        p = price(prop, fx)
        if p is None:
            continue
        cand = {"fixtureId": fx["id"], "fixture": fx, "proposition": prop, "label": describe(prop, fx),
                "fairYes": round(p, 4), "score": loc["score"] - abs(ko - start) / 86400 if start else loc["score"]}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best
