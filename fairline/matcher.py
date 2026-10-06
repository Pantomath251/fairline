"""Read a prediction-market question, find the real fixture it is about, and price it.

Handles the question shapes people actually write on Panta: "Will A beat B", "A will win against B with 2 or more
goals", "Will A beat B 3-2", "A will concede in the first 25 minutes against B", draws, totals, both-teams-to-score,
clean sheets, first goal and "A not to lose". Football questions are priced with the Poisson goal model; other sports
use the de-vigged moneyline. Anything the model cannot price faithfully (halves, corners, handicaps, outrights...)
returns no price: a missing price is better than a confidently wrong one."""
from __future__ import annotations

import math
import re
import unicodedata

from . import goals

STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "rc", "cd", "ud", "sd", "club", "de", "the", "calcio", "sk", "fk",
        "bk", "if", "cp", "sv", "vfb", "vfl", "tsg", "1", "04", "05", "09", "1899", "1846", "1907", "1910", "1913"}
GENERIC = {"united", "city", "town", "real", "sporting", "athletic", "atletico", "inter", "national", "women",
           "olympique", "racing", "dynamo", "dinamo", "red", "star", "young", "boys", "rovers", "wanderers", "county",
           "saint", "st", "san", "santa", "union", "deportivo", "independiente", "nacional", "central", "hotspur",
           "albion", "new", "los", "angeles", "york", "golden", "state", "bay", "las", "vegas", "north", "south",
           "east", "west", "fort", "port", "royal", "sport", "sports", "football", "soccer", "borussia"}
NICKNAMES = {
    "tottenham hotspur": ["spurs", "tottenham"], "manchester united": ["man utd", "man united", "manchester utd"],
    "manchester city": ["man city"], "paris saint-germain": ["psg", "paris sg", "paris"],
    "fc barcelona": ["barca", "barcelona"], "barcelona": ["barca"], "inter": ["inter milan", "internazionale"],
    "internazionale": ["inter", "inter milan"], "juventus": ["juve"], "fc bayern munchen": ["bayern", "bayern munich"],
    "bayern munchen": ["bayern", "bayern munich"], "atletico madrid": ["atleti", "atletico"],
    "wolverhampton": ["wolves"], "wolverhampton wanderers": ["wolves"], "brighton and hove albion": ["brighton"],
    "newcastle united": ["newcastle"], "west ham united": ["west ham"], "nottingham forest": ["forest", "nottm forest"],
    "borussia dortmund": ["dortmund", "bvb"], "bayer 04 leverkusen": ["leverkusen"], "bayer leverkusen": ["leverkusen"],
    "ac milan": ["milan"], "sl benfica": ["benfica"], "fc porto": ["porto"], "sporting cp": ["sporting lisbon"],
    "afc ajax": ["ajax"], "psv eindhoven": ["psv"], "united states": ["usa", "usmnt"],
    "werder bremen": ["bremen", "werder"], "aston villa": ["villa"], "leeds united": ["leeds"],
    "leicester city": ["leicester"], "ipswich town": ["ipswich"], "crystal palace": ["palace"],
}
SQUAD_TAG = re.compile(r"\b(women|womens|ladies|wfc|w|u\d{2}|ii|iii|reserves|youth)\b")
UNSUPPORTED = re.compile(
    r"\b(?:1st|2nd|first|second) half\b|half[- ]?time|\bht\b|quarter|\bperiod\b|overtime|\bot\b|extra time|penalt"
    r"|\bpoints?\b|spread|handicap|\bcover\b|\bexactly\b|to nil|corner|\bcards?\b|booking|\bshots?\b|possession"
    r"|scorer|hat[- ]?trick|assist|offside|\bvar\b|\bmvp\b|combined score|win the (?:league|title|cup|tournament|trophy)"
    r"|qualif|advance|relegat|promot|\btop \d|\bseason\b")
WIN = r"(?:beat|beats|defeat|defeats|win against|wins against|win vs|win over|win|wins|overcome|edge)"
SCORE = r"(?<![\d/.:-])(\d)\s*[-:]\s*(\d)(?![\d/.:-])(?!\s*(?:utc|gmt|bst|cet|cest|et|pt|am|pm|h)\b)"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^a-z0-9.:\-+/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def squad_tags(s: str) -> set[str]:
    tags = set()
    for t in SQUAD_TAG.findall(norm(s)):
        tags.add("women" if t in ("women", "womens", "ladies", "wfc", "w") else "reserves" if t in ("ii", "iii", "reserves") else t)
    return tags


def aliases(name: str, short: str | None = None, sport: str | None = None) -> list[str]:
    n = norm(name)
    tagged = bool(squad_tags(name))
    out = {n}
    if short:
        out.add(norm(short))
    core = " ".join(t for t in n.split() if t not in STOP)
    if core:
        out.add(core)
    for k, v in NICKNAMES.items():
        if norm(k) in (n, core):
            out.update(norm(x) for x in v)
    if tagged:
        # "Chelsea W" must still be found as "Chelsea Women" / "Chelsea Ladies" / "Chelsea FC Women"
        base = " ".join(t for t in core.split() if not SQUAD_TAG.fullmatch(t))
        for tag in squad_tags(name):
            for suffix in ({"women": ("women", "w", "ladies", "wfc", "fc women")}.get(tag) or
                           {"reserves": ("ii", "reserves", "b")}.get(tag) or (tag,)):
                out.add(f"{base} {suffix}")
    words = core.split()
    if sport == "tennis" and len(words) >= 2:
        out.add(words[-1])                       # surname
        out.add(f"{words[0][0]}. {words[-1]}")   # "j. sinner"
        out.add(f"{words[0][0]} {words[-1]}")
    elif not tagged:
        toks = [t for t in words if t not in GENERIC and len(t) >= 4]
        if len(toks) == 1:
            out.add(toks[0])  # "Arsenal", "Chelsea", "Liverpool", "Tottenham"
        elif toks and sport in ("basketball", "ice-hockey", "american-football", "baseball"):
            out.add(toks[-1])  # franchise nickname: "Los Angeles Lakers" -> "lakers"
    out.discard("")
    out = {re.sub(r"\s*\(.*?\)\s*", " ", a).strip() for a in out}
    return sorted((a for a in out if len(a) >= 3), key=len, reverse=True)


def locate(question: str, fx: dict) -> dict | None:
    """Template every mention of both teams as @H / @A. Requires both teams to be named."""
    q = norm(re.sub(r"\(.*?\)", " ", question))
    cands = []
    for tok, name, short in (("@H", fx["home"], fx.get("homeShort")), ("@A", fx["away"], fx.get("awayShort"))):
        for a in aliases(name, short, fx.get("sport")):
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", q):
                cands.append((m.start(), m.end(), tok, len(a)))
    chosen: list[tuple[int, int, str, int]] = []
    for c in sorted(cands, key=lambda c: (-c[3], c[0])):  # longest names win overlaps
        if all(c[1] <= o[0] or c[0] >= o[1] for o in chosen):
            chosen.append(c)
    toks = {c[2] for c in chosen}
    if toks != {"@H", "@A"}:
        return None
    t = q
    for s, e, tok, _ in sorted(chosen, reverse=True):
        t = t[:s] + tok + t[e:]
    score = max(c[3] for c in chosen if c[2] == "@H") + max(c[3] for c in chosen if c[2] == "@A")
    return {"text": t, "score": score}


def _subject(t: str) -> str | None:
    m = (re.search(r"(@H|@A)\s+(?:will\s+|to\s+|not\s+|fail to\s+)*(?:" + WIN + r"|lose|score|concede|keep|draw|avoid|not)\b", t)
         or re.search(r"\bwill (@H|@A)\b", t) or re.search(r"(@H|@A)", t))
    return m.group(1) if m else None


def parse(t: str) -> dict | None:
    """Classify a templated question. Returns {'kind', 'side', ...} with side in {'home','away'} where relevant."""
    if UNSUPPORTED.search(t):
        return None
    t = re.sub(r",?\s*or not\??$", "", t)
    side_of = {"@H": "home", "@A": "away"}
    other = {"home": "away", "away": "home"}
    subj = _subject(t)
    s = side_of.get(subj or "", "home")

    if re.search(r"both teams (?:to |will )?score|btts|both (?:@H|@A) and (?:@H|@A) (?:to |will )?score"
                 r"|(?:@H|@A) and (?:@H|@A) (?:to |will )?both score", t):
        return {"kind": "btts", "negate": bool(re.search(r"\bnot\b|\bfail\b|\bneither\b", t))}

    m = re.search(r"(@H|@A)\s+" + SCORE + r"\s+(@H|@A)", t)  # "@H 2-1 @A" reads left to right
    if m:
        x, y = int(m.group(2)), int(m.group(3))
        return {"kind": "exact", "home": x if m.group(1) == "@H" else y, "away": y if m.group(1) == "@H" else x}
    m = re.search(SCORE, t)
    if m and re.search(WIN + r"|\bscore|\bdraw|\bend|\bfinish", t):
        x, y = int(m.group(1)), int(m.group(2))
        # "A beat B 3-2": the score is the subject's; "A v B end 1-1": the first-named team's.
        persp = s if re.search(WIN, t) else side_of[re.search(r"@H|@A", t).group(0)]
        return {"kind": "exact", "home": x if persp == "home" else y, "away": y if persp == "home" else x}

    m = re.search(r"concede[sd]?\b.*?first (\d{1,3}) min", t) or re.search(r"concede[sd]? (?:a goal )?(?:before|within) (?:the )?(\d{1,3})(?:th)? min", t)
    if m:
        return {"kind": "team_scores", "side": other[s], "minutes": int(m.group(1))}
    m = re.search(r"score[sd]?\b.*?first (\d{1,3}) min", t) or re.search(r"score[sd]? (?:a goal )?(?:before|within) (?:the )?(\d{1,3})(?:th)? min", t)
    if m:
        return {"kind": "team_scores", "side": s, "minutes": int(m.group(1))}
    if re.search(r"(?:score[sd]?|net[s]?) (?:the )?first(?! \d)|first (?:goal|to score)|open(?:s)? the scoring", t):
        return {"kind": "first_goal", "side": s}

    if re.search(r"not lose|avoid defeat|unbeaten|draw or win|win or draw", t):
        return {"kind": "not_lose", "side": s}
    if re.search(r"\bnot (?:end |finish )?(?:in )?(?:a )?draw|no draw|have a winner", t):
        return {"kind": "draw", "negate": True}
    if re.search(r"\bdraw\b|\bdraws\b|\btie\b|end(?:s)? (?:in a )?(?:level|tie|draw)", t):
        return {"kind": "draw"}

    if re.search(r"clean sheet|keep (?:a )?clean|not concede|fail to score", t):
        if "fail to score" in t:
            return {"kind": "team_scores", "side": s, "negate": True}
        return {"kind": "clean_sheet", "side": s}

    line = under = None
    m = re.search(r"(?<!by )(over|more than|at least|under|fewer than|less than)\s*(\d+(?:\.\d+)?)\s*(?:total\s*)?goals?", t)
    if m:
        op, n = m.group(1), float(m.group(2))
        under = op in ("under", "fewer than", "less than")
        if op == "at least" or (under and n == int(n)):
            line = n - 0.5
        else:
            line = n
    else:
        m = re.search(r"(\d+)\s*(?:\+|or more)\s*goals?|(\d+)\s*goals? or more", t)
        if m and not re.search(r"\bby\b|\bwith\b", t[:m.start()][-12:]):
            line, under = int(m.group(1) or m.group(2)) - 0.5, False
    if line is not None:
        team_total = re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?score", t)
        if team_total:
            return {"kind": "team_total", "side": side_of[team_total.group(1)], "line": line, "negate": under}
        return {"kind": "total", "line": line, "negate": under}

    m = re.search(r"by (\d+) or more|by at least (\d+)|with (\d+) or more goals|by (\d+)\+|(\d+)\+ goals? (?:margin|difference)"
                  r"|by (\d+) goals? or more|by more than (\d+)", t)
    if m and re.search(WIN, t):
        groups = m.groups()
        k = int(next(g for g in groups if g))
        if groups[-1]:  # "by more than N"
            k += 1
        return {"kind": "margin", "side": s, "atLeast": k}

    neg = re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?(?:not|fail to|fails to)\s+" + WIN + r"\b", t) \
        or re.search(r"will (@H|@A) not " + WIN + r"\b", t)
    if neg:
        return {"kind": "win", "side": side_of[neg.group(1)], "negate": True}
    if re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?(?:" + WIN + r")\b", t) or re.search(r"will (@H|@A) " + WIN, t):
        return {"kind": "win", "side": s}
    m = re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?(?:lose|loses)\b", t)
    if m:
        return {"kind": "win", "side": other[side_of[m.group(1)]]}
    if re.search(r"(@H|@A)\s+(?:will\s+|to\s+)?score\b", t):
        return {"kind": "team_scores", "side": s}
    return None


def price(prop: dict, fx: dict) -> float | None:
    """Fair YES probability for a parsed proposition on a fixture."""
    fair = fx["fair"]
    kind = prop["kind"]
    if fx["sport"] != "football" or not fx.get("model"):
        if kind == "win" and fair.get("draw") is None:
            p = fair.get(prop["side"])
            return (1 - p if prop.get("negate") else p) if p is not None else None
        return None
    gm = goals.fit(*fx["modelFit"]) if fx.get("modelFit") else goals.fit(round(fair["home"], 4), round(fair["away"], 4))
    if kind == "win":
        p = fair[prop["side"]]
        return 1 - p if prop.get("negate") else p
    if kind == "draw":
        return 1 - fair["draw"] if prop.get("negate") else fair["draw"]
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
    if kind == "first_goal":
        lam = gm.lh if prop["side"] == "home" else gm.la
        tot = gm.lh + gm.la
        return lam / tot * (1 - math.exp(-tot)) if tot > 0 else None
    return None


def describe(prop: dict, fx: dict) -> str:
    team = {"home": fx["home"], "away": fx["away"]}
    k = prop["kind"]
    if k == "win":
        return f"{team[prop['side']]} {'do not ' if prop.get('negate') else ''}win"
    if k == "draw":
        return "Match has a winner (no draw)" if prop.get("negate") else "Match ends in a draw"
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
    if k == "first_goal":
        return f"{team[prop['side']]} score first"
    return k


def match_market(market: dict, fixtures: list[dict]) -> dict | None:
    """Best fixture + priced proposition for a Panta market, or None if it is not about a priced fixture.
    Only the title/question is read: descriptions often contain unrelated words ("not", "draw") that would flip
    the proposition."""
    text = " ".join(dict.fromkeys(x for x in (market.get("title"), market.get("question")) if x))
    if not text:
        return None
    q_tags = squad_tags(text)
    start = market.get("startTime") or 0
    end = market.get("endTime") or start
    best = None
    for fx in fixtures:
        ko = fx["startTimestamp"]
        if start and not (start - 36 * 3600 <= ko <= max(end, start) + 12 * 3600):
            continue
        if squad_tags(f"{fx['home']} {fx['away']}") != q_tags:
            continue  # women's / youth / reserve fixtures only match questions that say so
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
