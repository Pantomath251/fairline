"""Turn a real fixture into a well-specified Panta market (question, resolution rule, sources, times)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

MIN_START_DELAY = 3600  # Panta's on-chain minimumStartDelay (seconds)
SESSION_SLACK = 600     # create sessions live ~5 min; leave room for a slow wallet approval
MAX_QUESTION = 280      # on-chain QuestionTooLong limit

# The date makes each question unique (Panta derives the market address from creator + question) and never looks
# like a scoreline to the matcher ("on 10 Oct", not "10-10").
TEMPLATES = {
    "home_win": "Will {home} beat {away} on {day}?",
    "away_win": "Will {away} beat {home} on {day}?",
    "draw": "Will {home} vs {away} on {day} end in a draw?",
    "over25": "Will {home} vs {away} on {day} have over 2.5 goals?",
    "btts": "Will both {home} and {away} score on {day}?",
}


def question(fx: dict, kind: str) -> str:
    d = datetime.fromtimestamp(int(fx["startTimestamp"]), timezone.utc)
    q = TEMPLATES[kind].format(home=fx["home"], away=fx["away"], day=f"{d.day} {d:%b}")
    if len(q) > MAX_QUESTION:
        raise ValueError(f"question exceeds Panta's {MAX_QUESTION}-character on-chain limit")
    return q

REGULATION_SHORT = {
    "football": "in regular time (90'+stoppage; no ET/penalties)",
    "ice-hockey": "incl. OT and shootout",
    "basketball": "incl. OT",
    "american-football": "incl. OT",
    "baseball": "incl. extra innings",
    "tennis": "the match (retirement/walkover: the player who advances)",
}
# Bytes of question + rule + sources that fit one create transaction: a measured build with 330 text bytes came to
# 1035 of Solana's 1232 bytes, leaving ~200; keep ~45 bytes of margin.
ONCHAIN_TEXT_BUDGET = 480


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y %H:%M UTC").lstrip("0")


def kinds_for(fx: dict) -> list[str]:
    if fx["sport"] == "football":
        return ["home_win", "away_win", "draw", "over25", "btts"]
    return ["home_win", "away_win"]


def fair_yes(fx: dict, kind: str) -> float | None:
    f, m = fx["fair"], fx.get("model") or {}
    return {"home_win": f.get("home"), "away_win": f.get("away"), "draw": f.get("draw"),
            "over25": m.get("over25"), "btts": m.get("btts")}.get(kind)


def too_soon(fx: dict) -> bool:
    return int(fx["startTimestamp"]) - time.time() < MIN_START_DELAY + SESSION_SLACK


def build(fx: dict, kind: str, image_url: str = "", strict: bool = True) -> dict:
    """Panta create-quote payload for one proposition on a fixture (wallet added by the caller)."""
    if kind not in kinds_for(fx):
        raise ValueError(f"{kind} is not offered for {fx['sport']}")
    home, away, sport = fx["home"], fx["away"], fx["sport"]
    ko = int(fx["startTimestamp"])
    if strict and too_soon(fx):
        raise ValueError("Kick-off is less than an hour away; Panta requires startTime at least 3600s ahead")
    q = question(fx, kind)
    comp = fx.get("tournament") or "the competition"
    reg = REGULATION_SHORT.get(sport, "")
    rules = {
        "home_win": f"YES if {home} win {reg}; NO on a draw or {away} win.",
        "away_win": f"YES if {away} win {reg}; NO on a draw or {home} win.",
        "draw": "YES if level after regular time (90'+stoppage); NO otherwise.",
        "over25": "YES if 3+ goals in regular time (90'+stoppage), own goals count; NO otherwise.",
        "btts": f"YES if both {home} and {away} score in regular time (90'+stoppage); NO otherwise.",
    }
    sources = ["https://www.sofascore.com", "https://www.espn.com"]
    # Question, rule and sources are written on-chain by create_event and must fit one Solana transaction
    # (1232 bytes including accounts and signatures). Shorten the header, never the resolution terms.
    rule = None
    for head in (f"{home} v {away}, {comp}, {_when(ko)}.", f"{home} v {away}, {_when(ko)}.", f"Kickoff {_when(ko)}."):
        cand = f"{head} {rules[kind]} Abandoned or not played within 48h of kickoff: NO. Official result."
        if len(q.encode()) + len(cand.encode()) + sum(len(s) for s in sources) <= ONCHAIN_TEXT_BUDGET:
            rule = cand
            break
    if rule is None:
        raise ValueError("Team names are too long to fit an on-chain Panta market")
    end = ko + int(fx.get("durationMin", 150)) * 60
    payload = {
        "question": q,
        "title": q[:200],
        "description": f"{comp} · {_when(ko)}. Fair probability from de-vigged Pinnacle odds: "
                       f"{round((fair_yes(fx, kind) or 0) * 100, 1)}%. Created with Fairline.",
        "resolutionRule": rule,
        "sourcesOfTruth": sources[:20],
        "category": "sports",
        "startTime": ko,
        "endTime": end,
        "resolutionTime": end + 3600,
        "marketType": "standard",
        "region": "Global",
    }
    if image_url:
        payload["imageUrl"] = image_url
    return payload
