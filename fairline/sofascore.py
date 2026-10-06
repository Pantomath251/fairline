"""SofaScore public web API (the JSON the sofascore.com site itself loads): fixtures/results by date, live matches,
match details (statistics, tennis point-by-point, football incidents & lineups), betting odds, rankings and search."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from fractions import Fraction

from curl_cffi.requests import AsyncSession

API = "https://www.sofascore.com/api/v1"
SITE = "https://www.sofascore.com"
SPORTS = ["football", "tennis", "basketball", "ice-hockey", "baseball", "american-football", "volleyball", "handball",
          "cricket", "rugby", "table-tennis", "esports", "darts", "snooker", "futsal", "badminton", "waterpolo", "mma",
          "aussie-rules", "beach-volley", "floorball", "bandy", "minifootball"]
RANKINGS = {"atp": 5, "wta": 6}


class SofaError(Exception):
    pass


class Blocked(SofaError):
    pass


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def frac_to_decimal(f: str | None) -> float | None:
    try:
        return round(float(Fraction(f)) + 1, 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def event_id_from(v: str) -> int | None:
    v = str(v).strip()
    m = re.search(r"#id:(\d+)", v) or re.search(r"/event/(\d+)", v) or re.fullmatch(r"(\d{5,})", v)
    return int(m.group(1)) if m else None


def _team(t: dict) -> dict:
    return {
        "name": t.get("name"),
        "id": t.get("id"),
        "shortName": t.get("shortName"),
        "country": (t.get("country") or {}).get("name"),
        "ranking": t.get("ranking"),
    }


def _scores(sc: dict) -> dict:
    periods = {k: v for k, v in sc.items() if re.fullmatch(r"period\d+", k)}
    tbs = {k.replace("TieBreak", ""): v for k, v in sc.items() if k.endswith("TieBreak")}
    return {
        "current": sc.get("current"),
        "periods": [periods[k] for k in sorted(periods, key=lambda x: int(x[6:]))],
        "tiebreaks": {k: v for k, v in sorted(tbs.items())} or None,
        "point": sc.get("point"),
        "overtime": sc.get("overtime"),
        "penalties": sc.get("penalties"),
    }


def normalize_event(e: dict, sport: str | None = None) -> dict:
    t = e.get("tournament") or {}
    ut = t.get("uniqueTournament") or {}
    cat = t.get("category") or ut.get("category") or {}
    sp = sport or ((cat.get("sport") or {}).get("slug"))
    hs, as_ = _scores(e.get("homeScore") or {}), _scores(e.get("awayScore") or {})
    st = e.get("status") or {}
    home, away = _team(e.get("homeTeam") or {}), _team(e.get("awayTeam") or {})
    winner = {1: home["name"], 2: away["name"], 3: "draw"}.get(e.get("winnerCode"))
    row = {
        "type": "event",
        "id": e.get("id"),
        "url": f"{SITE}/{sp}/match/{e.get('slug')}/{e.get('customId')}#id:{e.get('id')}" if e.get("customId") else None,
        "sport": sp,
        "category": cat.get("name"),
        "tournament": t.get("name"),
        "uniqueTournament": ut.get("name"),
        "uniqueTournamentId": ut.get("id"),
        "season": (e.get("season") or {}).get("name"),
        "round": (e.get("roundInfo") or {}).get("name") or (e.get("roundInfo") or {}).get("round"),
        "startTime": _iso(e.get("startTimestamp")),
        "status": st.get("type"),
        "statusDescription": st.get("description"),
        "homeTeam": home["name"],
        "awayTeam": away["name"],
        "homeScore": hs["current"],
        "awayScore": as_["current"],
        "homePeriods": hs["periods"],
        "awayPeriods": as_["periods"],
        "homeTiebreaks": hs["tiebreaks"],
        "awayTiebreaks": as_["tiebreaks"],
        "homePoint": hs["point"],
        "awayPoint": as_["point"],
        "winner": winner,
        "home": home,
        "away": away,
        "groundType": e.get("groundType") or ut.get("groundType"),
        "tennisPoints": ut.get("tennisPoints"),
        "firstToServe": e.get("firstToServe"),
        "hasXg": e.get("hasXg"),
    }
    return row


def odds_fields(o: dict | None) -> dict:
    if not o:
        return {}
    out = {"oddsMarket": o.get("marketName"), "oddsIsLive": o.get("isLive")}
    for c in o.get("choices") or []:
        key = {"1": "oddsHome", "X": "oddsDraw", "2": "oddsAway"}.get(c.get("name"))
        if key:
            out[key] = frac_to_decimal(c.get("fractionalValue"))
            out[key + "Initial"] = frac_to_decimal(c.get("initialFractionalValue"))
    return out


def flatten_stats(d: dict) -> dict:
    """{'ALL': {'Aces': {'home': '3', 'away': '1'}, ...}, '1ST': {...}}"""
    out: dict = {}
    for per in d.get("statistics") or []:
        items = {}
        for g in per.get("groups") or []:
            for it in g.get("statisticsItems") or []:
                items[it.get("name")] = {"home": it.get("home"), "away": it.get("away")}
        out[per.get("period")] = items
    return out


def simplify_incidents(d: dict) -> list[dict]:
    keep = []
    for i in d.get("incidents") or []:
        if i.get("incidentType") in ("goal", "card", "substitution", "varDecision", "penaltyShootout", "period"):
            keep.append({
                "type": i.get("incidentType"),
                "class": i.get("incidentClass"),
                "minute": i.get("time"),
                "addedTime": i.get("addedTime") if i.get("addedTime") != 999 else None,
                "isHome": i.get("isHome"),
                "player": (i.get("player") or {}).get("name"),
                "assist": (i.get("assist1") or {}).get("name"),
                "playerIn": (i.get("playerIn") or {}).get("name"),
                "playerOut": (i.get("playerOut") or {}).get("name"),
                "homeScore": i.get("homeScore"),
                "awayScore": i.get("awayScore"),
                "text": i.get("text"),
            })
    return keep


def simplify_lineups(d: dict) -> dict:
    out = {"confirmed": d.get("confirmed")}
    for side in ("home", "away"):
        s = d.get(side) or {}
        out[side] = {
            "formation": s.get("formation"),
            "players": [{
                "name": (p.get("player") or {}).get("name"),
                "position": p.get("position") or (p.get("player") or {}).get("position"),
                "jerseyNumber": p.get("jerseyNumber") or p.get("shirtNumber"),
                "substitute": p.get("substitute"),
                "rating": ((p.get("statistics") or {}).get("rating")),
                "minutesPlayed": ((p.get("statistics") or {}).get("minutesPlayed")),
            } for p in s.get("players") or []],
        }
    return out


def simplify_pbp(d: dict) -> list[dict]:
    out = []
    for s in d.get("pointByPoint") or []:
        for g in s.get("games") or []:
            sc = g.get("score") or {}
            out.append({
                "set": s.get("set"),
                "game": g.get("game"),
                "server": {1: "home", 2: "away"}.get(sc.get("serving")),
                "gameWinner": {1: "home", 2: "away"}.get(sc.get("scoring")),
                "homeGames": sc.get("homeScore"),
                "awayGames": sc.get("awayScore"),
                "points": [f"{p.get('homePoint')}-{p.get('awayPoint')}" for p in g.get("points") or []],
            })
    out.sort(key=lambda x: (x["set"] or 0, x["game"] or 0))
    return out


def normalize_ranking(r: dict, tour: str) -> dict:
    t = r.get("team") or {}
    return {
        "type": "ranking",
        "tour": tour.upper(),
        "rank": r.get("ranking"),
        "player": t.get("name"),
        "playerId": t.get("id"),
        "country": (t.get("country") or {}).get("name"),
        "points": r.get("points"),
        "previousRank": r.get("previousRanking"),
        "bestRank": r.get("bestRanking"),
        "tournamentsPlayed": r.get("tournamentsPlayed"),
        "url": f"{SITE}/{'tennis/player' if tour in RANKINGS else 'team'}/{t.get('slug')}/{t.get('id')}" if t.get("slug") else None,
    }


class SofaClient:
    def __init__(self, proxy_cfg=None, concurrency: int = 6):
        self.proxy_cfg = proxy_cfg
        self.sem = asyncio.Semaphore(concurrency)
        self._s: AsyncSession | None = None
        self._retired: list[AsyncSession] = []

    async def _session(self, fresh: bool = False) -> AsyncSession:
        if self._s is None or fresh:
            # Other coroutines may still be using the current session: retire it instead of closing it under them.
            if self._s is not None:
                self._retired.append(self._s)
            proxy = await self.proxy_cfg.new_url() if self.proxy_cfg else None
            self._s = AsyncSession(impersonate="chrome", proxy=proxy, timeout=30,
                                   headers={"referer": SITE + "/", "accept-language": "en-US,en;q=0.9"})
        return self._s

    async def close(self):
        for s in [*self._retired, self._s]:
            if s is not None:
                try:
                    await s.close()
                except Exception:  # noqa: BLE001
                    pass
        self._s, self._retired = None, []

    async def get(self, path: str, allow_404: bool = True) -> dict | None:
        err: Exception = SofaError(path)
        async with self.sem:
            for attempt in range(4):
                s = await self._session(fresh=attempt > 0)
                try:
                    r = await s.get(API + path)
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code == 404 and allow_404:
                        return None
                    err = Blocked(f"{path}: HTTP {r.status_code} {r.text[:60]}") if r.status_code in (403, 429) else SofaError(f"{path}: HTTP {r.status_code}")
                except Exception as e:  # noqa: BLE001
                    err = Blocked(f"{path}: {type(e).__name__}")
                await asyncio.sleep(1.2 * (attempt + 1))
        raise err

    async def events_by_date(self, sport: str, date: str) -> list[dict]:
        cats = await self.get(f"/sport/{sport}/{date}/0/categories") or {}
        ids = [c["category"]["id"] for c in cats.get("categories") or [] if c.get("totalEvents")]
        res = await asyncio.gather(*(self.get(f"/category/{cid}/scheduled-events/{date}") for cid in ids))
        seen, out = set(), []
        day = date
        for d in res:
            for e in (d or {}).get("events") or []:
                # the category feed includes neighbouring days around midnight; keep the requested UTC date
                if e["id"] in seen or (_iso(e.get("startTimestamp")) or "")[:10] != day:
                    continue
                seen.add(e["id"])
                out.append(e)
        return out

    async def live(self, sport: str) -> list[dict]:
        return (await self.get(f"/sport/{sport}/events/live") or {}).get("events") or []

    async def odds_by_date(self, sport: str, date: str) -> dict:
        return (await self.get(f"/sport/{sport}/odds/1/{date}") or {}).get("odds") or {}

    async def event(self, eid: int) -> dict | None:
        return (await self.get(f"/event/{eid}") or {}).get("event")

    async def event_odds(self, eid: int) -> dict | None:
        return ((await self.get(f"/event/{eid}/odds/1/featured") or {}).get("featured") or {}).get("default")

    async def details(self, row: dict, pbp: bool, lineups: bool) -> dict:
        eid, sport = row["id"], row["sport"]
        jobs = {"statistics": self.get(f"/event/{eid}/statistics"), "votes": self.get(f"/event/{eid}/votes")}
        if sport == "tennis" and pbp:
            jobs["pointByPoint"] = self.get(f"/event/{eid}/point-by-point")
        if sport != "tennis":
            jobs["incidents"] = self.get(f"/event/{eid}/incidents")
            if lineups:
                jobs["lineups"] = self.get(f"/event/{eid}/lineups")
        res = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
        out: dict = {}
        st = res.get("statistics")
        out["statistics"] = flatten_stats(st) if isinstance(st, dict) else None
        v = res.get("votes")
        vote = (v or {}).get("vote") if isinstance(v, dict) else None
        out["fanVotes"] = {"home": vote.get("vote1"), "draw": vote.get("voteX"), "away": vote.get("vote2")} if vote else None
        if "pointByPoint" in res:
            out["pointByPoint"] = simplify_pbp(res["pointByPoint"]) if isinstance(res["pointByPoint"], dict) else None
        if "incidents" in res:
            out["incidents"] = simplify_incidents(res["incidents"]) if isinstance(res["incidents"], dict) else None
        if "lineups" in res:
            out["lineups"] = simplify_lineups(res["lineups"]) if isinstance(res["lineups"], dict) else None
        return out

    async def rankings(self, tour: str) -> list[dict]:
        d = await self.get(f"/rankings/type/{RANKINGS[tour]}") or {}
        return [normalize_ranking(r, tour) for r in d.get("rankings") or []]

    async def search(self, q: str) -> list[dict]:
        d = await self.get(f"/search/all?q={q}") or {}
        out = []
        for r in d.get("results") or []:
            e = r.get("entity") or {}
            sport = ((e.get("sport") or {}).get("slug") or ((e.get("team") or {}).get("sport") or {}).get("slug")
                     or ((e.get("category") or {}).get("sport") or {}).get("slug"))
            kind = r.get("type")
            out.append({
                "type": "searchResult",
                "query": q,
                "entityType": kind,
                "name": e.get("name"),
                "id": e.get("id"),
                "sport": sport,
                "country": (e.get("country") or {}).get("name"),
                "team": (e.get("team") or {}).get("name"),
                "url": f"{SITE}/{sport + '/' if sport else ''}{kind}/{e.get('slug')}/{e.get('id')}" if e.get("slug") else None,
                "userCount": e.get("userCount"),
            })
        return out
