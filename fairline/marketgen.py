"""Turn a real fixture into a well-specified Panta market (question, resolution rule, sources, times)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

MIN_START_DELAY = 3600  # Panta's on-chain minimumStartDelay (seconds)

TEMPLATES = {
    "home_win": "Will {home} beat {away}?",
    "away_win": "Will {away} beat {home}?",
    "draw": "Will {home} vs {away} end in a draw?",
    "over25": "Will {home} vs {away} have over 2.5 goals?",
    "btts": "Will both {home} and {away} score?",
}

REGULATION = {
    "football": "after regular time (90 minutes plus stoppage time; extra time and penalty shoot-outs do not count)",
    "ice-hockey": "including overtime and shoot-out",
    "basketball": "including overtime",
    "tennis": "(a retirement or walkover counts as a win for the player who advances)",
}


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y %H:%M UTC")


def kinds_for(fx: dict) -> list[str]:
    if fx["sport"] == "football":
        return ["home_win", "away_win", "draw", "over25", "btts"]
    return ["home_win", "away_win"]


def fair_yes(fx: dict, kind: str) -> float | None:
    f, m = fx["fair"], fx.get("model") or {}
    return {"home_win": f.get("home"), "away_win": f.get("away"), "draw": f.get("draw"),
            "over25": m.get("over25"), "btts": m.get("btts")}.get(kind)


def too_soon(fx: dict) -> bool:
    return int(fx["startTimestamp"]) - time.time() < MIN_START_DELAY + 120


def build(fx: dict, kind: str, image_url: str = "", strict: bool = True) -> dict:
    """Panta create-quote payload for one proposition on a fixture (wallet added by the caller)."""
    if kind not in kinds_for(fx):
        raise ValueError(f"{kind} is not offered for {fx['sport']}")
    home, away, sport = fx["home"], fx["away"], fx["sport"]
    ko = int(fx["startTimestamp"])
    if strict and too_soon(fx):
        raise ValueError("Kick-off is less than an hour away; Panta requires startTime at least 3600s ahead")
    question = TEMPLATES[kind].format(home=home, away=away)
    comp = fx.get("tournament") or "the competition"
    reg = REGULATION.get(sport, "")
    base = f"The {comp} match {home} vs {away} scheduled for {_when(ko)}"
    rules = {
        "home_win": f"Resolves YES if {home} win the match {reg}. Resolves NO if the match ends in a draw or {away} win.",
        "away_win": f"Resolves YES if {away} win the match {reg}. Resolves NO if the match ends in a draw or {home} win.",
        "draw": f"Resolves YES if the match is level at the end of regular time (90 minutes plus stoppage time). "
                f"Resolves NO otherwise.",
        "over25": "Resolves YES if 3 or more goals are scored in regular time (90 minutes plus stoppage time), "
                  "own goals included. Resolves NO if 2 or fewer goals are scored.",
        "btts": f"Resolves YES if both {home} and {away} score at least one goal in regular time "
                f"(90 minutes plus stoppage time), own goals credited to the benefiting team. Resolves NO otherwise.",
    }
    rule = (f"{base}. {rules[kind]} If the match is abandoned, or postponed and not completed within 48 hours of the "
            f"scheduled start, the market resolves NO. Official result as published by the organiser and on "
            f"SofaScore.")
    end = ko + int(fx.get("durationMin", 150)) * 60
    sources = [s for s in (fx.get("url"), "https://www.sofascore.com", "https://www.espn.com/soccer/scoreboard"
                           if sport == "football" else "https://www.espn.com") if s]
    payload = {
        "question": question[:512],
        "title": question[:200],
        "description": f"{comp} · {_when(ko)}. Fair probability from bookmaker consensus (de-vigged): "
                       f"{round((fair_yes(fx, kind) or 0) * 100, 1)}%. Created with Fairline.",
        "resolutionRule": rule[:2048],
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
