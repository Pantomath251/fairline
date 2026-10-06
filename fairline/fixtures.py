"""Upcoming fixtures with sharp bookmaker odds turned into fair probabilities.

Primary source: Pinnacle (lowest-margin book; moneyline + totals). Fallback: SofaScore's consensus odds."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, goals, odds
from .pinnacle import PinnacleClient
from .sofascore import SofaClient, normalize_event, odds_fields

log = logging.getLogger("fairline.fixtures")

# Expected wall-clock length of a match, used for Panta endTime and for matching markets to fixtures.
DURATION_MIN = {"football": 115, "basketball": 150, "tennis": 180, "ice-hockey": 160, "american-football": 210,
                "baseball": 200, "handball": 90, "volleyball": 120}


def finish(raw: dict) -> dict | None:
    """Add fair probabilities (and, for football, the fitted goal model) to a raw fixture with decimal odds."""
    if not raw.get("oddsHome") or not raw.get("oddsAway"):
        return None
    fair = odds.fair_from_fixture(raw)
    if not fair:
        return None
    fx = {k: v for k, v in raw.items() if k not in ("oddsHome", "oddsDraw", "oddsAway", "total")}
    fx["odds"] = {"home": raw.get("oddsHome"), "draw": raw.get("oddsDraw"), "away": raw.get("oddsAway")}
    fx["fair"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fair.items()}
    fx["durationMin"] = DURATION_MIN.get(raw["sport"], 150)
    tot = raw.get("total")
    p_over = None
    if tot and tot.get("line") is not None and tot.get("over") and tot.get("under"):
        dv = odds.devig_power([tot["over"], tot["under"]])
        if dv:
            p_over = round(dv[0], 4)
            fx["total"] = {"line": tot["line"], "over": tot["over"], "under": tot["under"], "fairOver": p_over}
    if raw["sport"] == "football" and fair.get("draw") is not None:
        line = float(tot["line"]) if p_over is not None else None
        if line is not None and (line * 2) % 2 != 1:  # only half-goal lines (x.5) are pure probabilities
            line, p_over = None, None
        fx["modelFit"] = [round(fair["home"], 4), round(fair["away"], 4), line, p_over]
        fx["model"] = goals.fit(*fx["modelFit"]).as_dict()
    return fx


def _sofa_fixture(e: dict, sport: str, odds_row: dict | None) -> dict | None:
    r = normalize_event(e, sport)
    r.update(odds_fields(odds_row))
    tour = e.get("tournament") or {}
    return finish({
        "source": "sofascore",
        "priority": int(tour.get("priority") or (tour.get("uniqueTournament") or {}).get("priority") or 0),
        "id": r["id"],
        "sport": sport,
        "tournament": r.get("uniqueTournament") or r.get("tournament"),
        "category": r.get("category"),
        "home": r["homeTeam"],
        "away": r["awayTeam"],
        "homeShort": (r.get("home") or {}).get("shortName"),
        "awayShort": (r.get("away") or {}).get("shortName"),
        "startTimestamp": int(e.get("startTimestamp") or 0),
        "startTime": r.get("startTime"),
        "status": r.get("status"),
        "url": r.get("url"),
        "oddsHome": r.get("oddsHome"), "oddsDraw": r.get("oddsDraw"), "oddsAway": r.get("oddsAway"),
    })


class FixtureBook:
    """Caches fixtures for the next few days and refreshes them in the background."""

    def __init__(self, sports: list[str] | None = None, days: int | None = None, ttl: float = 1800,
                 cache_path: Path | None = None):
        self.sports = sports or config.FIXTURE_SPORTS
        self.days = days or config.FIXTURE_DAYS
        self.ttl = ttl
        self.cache_path = cache_path or config.ROOT / "data" / "fixtures.json"
        self.items: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.updated = 0.0
        self._lock = asyncio.Lock()
        self._load_cache()

    def _set(self, items: list[dict], updated: float) -> None:
        self.items = sorted(items, key=lambda f: f["startTimestamp"])
        self.by_id = {f["id"]: f for f in self.items}
        self.updated = updated

    def _load_cache(self) -> None:
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._set(d["items"], d["updated"])
            log.info("fixtures cache loaded: %d", len(self.items))
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"updated": self.updated, "items": self.items}), encoding="utf-8")
        except OSError as e:
            log.warning("fixtures cache not saved: %s", e)

    async def _load_day(self, sc: SofaClient, sport: str, day: str) -> list[dict] | None:
        """Fixtures with odds for one sport/day, or None when the source failed (keep the previous data)."""
        try:
            evs, od = await asyncio.gather(sc.events_by_date(sport, day), sc.odds_by_date(sport, day))
        except Exception as e:  # noqa: BLE001 - one bad sport/day must not sink the board
            log.warning("fixtures %s %s failed: %s", sport, day, e)
            return None
        out = []
        for e in evs:
            if (e.get("status") or {}).get("type") != "notstarted":
                continue
            fx = _sofa_fixture(e, sport, od.get(str(e.get("id"))))
            if fx:
                out.append(fx)
        return out

    async def _load_pinnacle(self, pc: PinnacleClient, sport: str) -> list[dict] | None:
        try:
            raw = await pc.fixtures(sport, self.days)
        except Exception as e:  # noqa: BLE001
            log.warning("pinnacle %s failed: %s", sport, e)
            return None
        return [fx for fx in (finish(r) for r in raw) if fx]

    async def _load_sofascore(self, sport: str) -> list[dict] | None:
        today = datetime.now(timezone.utc).date()
        days = [(today + timedelta(days=i)).isoformat() for i in range(self.days)]
        sc = SofaClient(concurrency=4)
        try:
            res = await asyncio.gather(*(self._load_day(sc, sport, d) for d in days))
        finally:
            await sc.close()
        if all(r is None for r in res):
            return None
        return [fx for r in res if r for fx in r]

    async def refresh(self, max_age: float = 0.0) -> None:
        async with self._lock:
            if self.items and time.time() - self.updated < max_age:
                return  # another caller refreshed while we waited for the lock
            pc = PinnacleClient()
            fresh, failed = [], set()
            try:
                for sport in self.sports:
                    got = await self._load_pinnacle(pc, sport)
                    if not got:  # Pinnacle down for this sport -> SofaScore consensus odds
                        got = await self._load_sofascore(sport)
                    if got is None:
                        failed.add(sport)
                    else:
                        fresh += got
            finally:
                await pc.close()
            # keep earlier data for sports whose sources all failed this round
            kept = [f for f in self.items if f["sport"] in failed]
            if fresh or kept:
                self._set(fresh + kept, time.time())
                self._save_cache()
            log.info("fixtures refreshed: %d new, %d kept, failed sports: %s", len(fresh), len(kept),
                     sorted(failed) or "none")

    async def get(self) -> list[dict]:
        if not self.items:
            await self.refresh(max_age=self.ttl)
        now = time.time()
        return [f for f in self.items if f["startTimestamp"] > now]

    async def run_forever(self) -> None:
        if self.items and time.time() - self.updated < self.ttl:
            await asyncio.sleep(self.ttl - (time.time() - self.updated))
        while True:
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("fixture refresh failed: %s", e)
            await asyncio.sleep(self.ttl)
