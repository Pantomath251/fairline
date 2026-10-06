"""Unit tests for the pricing core and the on-chain decoder. Run: python -m pytest -q"""
import base64
from pathlib import Path

from fairline import goals, marketgen, matcher, odds
from fairline.anchor import Idl

IDL = Idl.load(Path(__file__).resolve().parent.parent / "fairline" / "idl" / "balr_market.json")


def fixture(home, away, oh, od, oa, sport="football", ts=4102444800):
    f = {"id": 1, "sport": sport, "home": home, "away": away, "homeShort": None, "awayShort": None,
         "startTimestamp": ts, "tournament": "Test League", "durationMin": 115, "url": None,
         "fair": odds.fair_from_fixture({"oddsHome": oh, "oddsDraw": od, "oddsAway": oa})}
    if sport == "football":
        f["model"] = goals.fit(round(f["fair"]["home"], 4), round(f["fair"]["away"], 4)).as_dict()
    return f


def test_devig_sums_to_one_and_keeps_order():
    p = odds.devig_power([1.9, 3.6, 4.2])
    assert abs(sum(p) - 1) < 1e-9
    assert p[0] > p[2] > 0 and p[1] > 0
    assert odds.overround([2.0, 2.0]) == 0.0


def test_edge_sign():
    e = odds.edge(0.60, 0.45, fee_bps=200)
    assert e["bestSide"] == "yes" and e["value"] and e["yes"]["ev"] > 0 and 0 < e["yes"]["kelly"] < 1
    e = odds.edge(0.30, 0.45, fee_bps=200)
    assert e["bestSide"] == "no"


def test_goal_model_matches_targets():
    gm = goals.fit(0.5124, 0.2243)
    r = gm.result()
    assert abs(r["home"] - 0.5124) < 0.01 and abs(r["away"] - 0.2243) < 0.01
    assert abs(sum(r.values()) - 1) < 1e-6
    assert gm.margin("home", 2) < r["home"]
    assert 0 < gm.exact(1, 0) < 1


def test_matcher_question_shapes():
    fx = [fixture("Manchester United", "Tottenham Hotspur", 2.3, 3.5, 3.0), fixture("France", "Belgium", 1.9, 3.6, 4.2)]
    cases = {
        "Manchester United will win against Spurs with 2 or more goals": "margin",
        "France will concede in the first 25 minutes against Belgium": "team_scores",
        "Will France and Belgium draw?": "draw",
        "Will France beat Belgium 2-1?": "exact",
        "France vs Belgium over 2.5 goals?": "total",
        "Will Belgium not lose to France?": "not_lose",
        "Will France beat Belgium?": "win",
    }
    for q, kind in cases.items():
        m = matcher.match_market({"title": q, "startTime": 0}, fx)
        assert m and m["proposition"]["kind"] == kind, q
        assert 0 < m["fairYes"] < 1
    assert matcher.match_market({"title": "Will it rain in London?", "startTime": 0}, fx) is None


def test_concede_maps_to_opponent_scoring():
    fx = [fixture("France", "Belgium", 1.9, 3.6, 4.2)]
    m = matcher.match_market({"title": "France will concede in the first 25 minutes against Belgium", "startTime": 0}, fx)
    assert m["proposition"]["side"] == "away" and m["proposition"]["minutes"] == 25


def test_marketgen_payload():
    fx = fixture("Arsenal", "Chelsea", 1.8, 3.8, 4.5)
    p = marketgen.build(fx, "home_win", "https://example.com/x.png")
    assert p["question"] == "Will Arsenal beat Chelsea?"
    assert p["startTime"] < p["endTime"] <= p["resolutionTime"]
    assert p["category"] == "sports" and p["sourcesOfTruth"] and p["imageUrl"]


def test_pinnacle_odds_and_finish():
    from fairline.fixtures import finish
    from fairline.pinnacle import american_to_decimal
    assert american_to_decimal(150) == 2.5 and american_to_decimal(-200) == 1.5 and american_to_decimal(None) is None
    fx = finish({"source": "pinnacle", "id": 7, "sport": "football", "home": "A", "away": "B", "startTimestamp": 4102444800,
                 "oddsHome": 2.1, "oddsDraw": 3.4, "oddsAway": 3.6, "total": {"line": 2.5, "over": 1.95, "under": 1.95}})
    assert abs(fx["total"]["fairOver"] - 0.5) < 1e-6
    assert abs(fx["model"]["over25"] - 0.5) < 0.03  # model honours the totals market
    q = finish({"source": "pinnacle", "id": 8, "sport": "football", "home": "A", "away": "B", "startTimestamp": 4102444800,
                "oddsHome": 2.1, "oddsDraw": 3.4, "oddsAway": 3.6, "total": {"line": 2.25, "over": 1.9, "under": 2.0}})
    assert q["modelFit"][2] is None  # quarter lines are not used as probabilities


def test_anchor_event_roundtrip():
    # EventResolved { event: pubkey, yes_wins: bool, timestamp: i64 }
    disc = next(k for k, v in IDL.events.items() if v == "EventResolved")
    data = disc + bytes(range(32)) + b"\x01" + (1791235770).to_bytes(8, "little", signed=True)
    logs = [f"Program {IDL.address} invoke [1]", "Program log: Instruction: ResolveEventUsdc",
            "Program data: " + base64.b64encode(data).decode(), f"Program {IDL.address} success"]
    ev = IDL.decode_logs(logs)
    assert ev and ev[0]["name"] == "EventResolved" and ev[0]["data"]["yes_wins"] is True
    assert ev[0]["data"]["timestamp"] == 1791235770
    assert IDL.instruction_names(logs) == ["ResolveEventUsdc"]
