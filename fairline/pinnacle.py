"""Pinnacle odds via the public guest API that powers pinnacle.com.

Pinnacle is the sharpest, lowest-margin sportsbook; its closing lines are the industry's reference for "true"
probabilities. Two requests per sport return every matchup and every straight market (moneyline, totals, spreads,
team totals)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("fairline.pinnacle")

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
# Public key embedded in pinnacle.com's own front end for anonymous (guest) reads.
GUEST_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
SPORTS = {"football": 29, "basketball": 4, "tennis": 33, "ice-hockey": 19, "american-football": 15, "baseball": 3}
TOP = ("premier league", "la liga", "laliga", "serie a", "bundesliga", "ligue 1", "champions league", "europa league",
       "conference league", "uefa", "fifa", "world cup", "nations league", "copa", "mls", "eredivisie", "primeira",
       "nba", "euroleague", "nhl", "atp", "wta", "nfl", "ncaa", "mlb", "brazil - serie a", "argentina - liga")


def american_to_decimal(p: float | int | None) -> float | None:
    if p is None:
        return None
    p = float(p)
    if p == 0:
        return None
    return round(1 + (p / 100 if p > 0 else 100 / abs(p)), 4)


def _priority(league: dict) -> int:
    name = (league.get("name") or "").lower()
    score = 0
    if any(k in name for k in TOP):
        score += 300
    if league.get("isFeatured"):
        score += 200
    if league.get("isPromoted"):
        score += 100
    if "women" in name or "u21" in name or "u19" in name or "reserves" in name:
        score -= 150
    return score + min(int(league.get("matchupCount") or 0), 50)


class PinnacleClient:
    def __init__(self, timeout: float = 30):
        self._http = httpx.AsyncClient(timeout=timeout, headers={
            "X-API-Key": GUEST_KEY, "Referer": "https://www.pinnacle.com/", "Origin": "https://www.pinnacle.com",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/140.0 Safari/537.36"})

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str):
        for attempt in range(3):
            r = await self._http.get(BASE + path)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503):
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"pinnacle {path}: HTTP {r.status_code}")
        raise RuntimeError(f"pinnacle {path}: retries exhausted")

    async def fixtures(self, sport: str, horizon_days: int = 4) -> list[dict]:
        sid = SPORTS[sport]
        matchups, markets = await asyncio.gather(
            self._get(f"/sports/{sid}/matchups?withSpecials=false"),
            self._get(f"/sports/{sid}/markets/straight?primaryOnly=false&withSpecials=false"))
        by_m: dict[int, list[dict]] = {}
        for mk in markets:
            if mk.get("period") == 0 and mk.get("status") == "open":
                by_m.setdefault(mk["matchupId"], []).append(mk)
        now = datetime.now(timezone.utc).timestamp()
        units = ("Sets",) if sport == "tennis" else (None, "Regular")  # skip corners/bookings/games side-matchups
        out = []
        for m in matchups:
            if m.get("type") != "matchup" or m.get("parentId") or m.get("isLive") or m.get("units") not in units:
                continue
            try:
                ts = int(datetime.fromisoformat(m["startTime"].replace("Z", "+00:00")).timestamp())
            except (KeyError, ValueError):
                continue
            if not now < ts < now + horizon_days * 86400:
                continue
            teams = {p.get("alignment"): p.get("name") for p in m.get("participants") or []}
            if not teams.get("home") or not teams.get("away"):
                continue
            fx = self._fixture(m, sport, ts, teams, by_m.get(m["id"], []))
            if fx:
                out.append(fx)
        return out

    @staticmethod
    def _fixture(m: dict, sport: str, ts: int, teams: dict, mks: list[dict]) -> dict | None:
        ml = next((x for x in mks if x.get("type") == "moneyline" and not x.get("isAlternate")), None)
        if not ml:
            return None
        px = {p.get("designation"): american_to_decimal(p.get("price")) for p in ml.get("prices") or []}
        if not px.get("home") or not px.get("away"):
            return None
        total = next((x for x in mks if x.get("type") == "total" and not x.get("isAlternate")), None)
        tot = None
        if total:
            tp = {p.get("designation"): p for p in total.get("prices") or []}
            if "over" in tp and "under" in tp:
                tot = {"line": tp["over"].get("points"), "over": american_to_decimal(tp["over"].get("price")),
                       "under": american_to_decimal(tp["under"].get("price"))}
        league = m.get("league") or {}
        return {
            "source": "pinnacle",
            "id": int(m["id"]),
            "sport": sport,
            "tournament": league.get("name"),
            "category": league.get("group"),
            "priority": _priority(league),
            "home": teams["home"],
            "away": teams["away"],
            "homeShort": None,
            "awayShort": None,
            "startTimestamp": ts,
            "startTime": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "status": "notstarted",
            "url": None,
            "oddsHome": px.get("home"),
            "oddsDraw": px.get("draw"),
            "oddsAway": px.get("away"),
            "total": tot,
        }
