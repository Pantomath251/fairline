"""Agent API: AI agents price prediction-market questions and find mispriced Panta markets, paying per call
with x402 (USDC). With no PAY_TO configured the routes stay open and are flagged as demo."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import config, matcher

log = logging.getLogger("fairline.agent")


def _paid(price: str, description: str, example_input: dict, schema: dict, example_output: dict) -> dict:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    return {
        "accepts": {"scheme": "exact", "payTo": config.PAY_TO, "price": price, "network": config.X402_NETWORK},
        "serviceName": "Fairline Agent API",
        "description": description,
        "mimeType": "application/json",
        "extensions": declare_discovery_extension(input=example_input, input_schema=schema,
                                                  output=OutputConfig(example=example_output)),
    }


CATALOG = [
    ("GET /agent/v1/price", "$0.003",
     "Fair probability for a free-text sports prediction-market question (win, draw, margin, exact score, totals, "
     "both teams to score, early goals), priced from de-vigged bookmaker consensus and a Poisson goal model.",
     {"question": "Will Arsenal beat Chelsea by 2 or more goals?"},
     {"properties": {"question": {"type": "string"}, "start": {"type": "string", "description": "unix seconds, optional"}},
      "required": ["question"]},
     {"matched": True, "label": "Arsenal win by 2+", "fairYes": 0.2871}),
    ("GET /agent/v1/fixtures", "$0.002",
     "Upcoming fixtures (next days) with bookmaker odds, de-vigged fair probabilities and expected goals.",
     {"sport": "football", "q": "premier league"},
     {"properties": {"sport": {"type": "string"}, "q": {"type": "string"}}},
     {"fixtures": [{"home": "Arsenal", "away": "Chelsea", "fair": {"home": 0.51, "draw": 0.26, "away": 0.23}}]}),
    ("GET /agent/v1/edges", "$0.005",
     "Live Panta prediction markets whose price disagrees with the fair probability, sorted by expected value.",
     {"min_ev": "0.05"},
     {"properties": {"min_ev": {"type": "string"}}},
     {"markets": [{"title": "Will Arsenal beat Chelsea?", "edge": {"bestSide": "yes", "yes": {"fair": 0.54, "price": 0.45, "ev": 0.18}}}]}),
]


def mount(app: FastAPI, book, markets_fn) -> None:
    routes = {}
    if config.PAY_TO:
        try:
            routes = {key: _paid(price, desc, ex_in, schema, ex_out) for key, price, desc, ex_in, schema, ex_out in CATALOG}
            if config.PUBLIC_URL:
                for key, cfg in routes.items():
                    cfg["resource"] = config.PUBLIC_URL + key.split()[1]
            from x402 import x402ResourceServer
            from x402.http import FacilitatorConfig, HTTPFacilitatorClient
            from x402.http.middleware.fastapi import payment_middleware
            server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=config.FACILITATOR_URL)))
            if config.X402_NETWORK.startswith("solana:"):
                from x402.mechanisms.svm.exact import register_exact_svm_server
                register_exact_svm_server(server, config.X402_NETWORK)
            else:
                from x402.mechanisms.evm.exact import register_exact_evm_server
                register_exact_evm_server(server, config.X402_NETWORK)
            mw = payment_middleware(routes, server, sync_facilitator_on_start=True)

            @app.middleware("http")
            async def x402_mw(request, call_next):
                if not request.url.path.startswith("/agent/v1/"):
                    return await call_next(request)
                try:
                    return await mw(request, call_next)
                except Exception as e:  # noqa: BLE001 - a facilitator outage must not become a generic 500
                    log.warning("x402 middleware failed: %s", e)
                    return JSONResponse({"error": "payments temporarily unavailable"}, status_code=503)
            log.info("x402 payments enabled on %s -> %s", config.X402_NETWORK, config.PAY_TO)
        except BaseException as e:  # noqa: BLE001 - payments are optional; never take the app down (incl. SystemExit)
            if isinstance(e, KeyboardInterrupt):
                raise
            log.warning("x402 disabled: %s", e)
            routes = {}

    paid_on = bool(routes)

    @app.get("/agent", tags=["agent"])
    async def agent_catalog():
        base = config.PUBLIC_URL
        return {
            "name": "Fairline Agent API",
            "payment": {"protocol": "x402", "network": config.X402_NETWORK, "asset": "USDC",
                        "payTo": config.PAY_TO or None, "enabled": paid_on},
            "endpoints": [{"method": k.split()[0], "path": base + k.split()[1], "price": p, "description": d}
                          for k, p, d, *_ in CATALOG],
        }

    @app.get("/.well-known/x402", include_in_schema=False)
    async def well_known():
        return {"x402Version": 2, "resources": [config.PUBLIC_URL + k.split()[1] for k, *_ in CATALOG],
                "network": config.X402_NETWORK, "payTo": config.PAY_TO or None}

    @app.get("/agent/v1/price", tags=["agent"])
    async def agent_price(question: str, start: int = 0):
        m = matcher.match_market({"title": question, "startTime": start}, await book.get())
        if not m:
            return {"matched": False, "demo": not paid_on}
        fx = m["fixture"]
        return {"matched": True, "demo": not paid_on, "label": m["label"], "fairYes": m["fairYes"],
                "fairNo": round(1 - m["fairYes"], 4), "proposition": m["proposition"],
                "fixture": {k: fx[k] for k in ("home", "away", "tournament", "startTime", "sport", "odds", "fair")},
                "model": fx.get("model")}

    @app.get("/agent/v1/fixtures", tags=["agent"])
    async def agent_fixtures(sport: str = "", q: str = "", limit: int = Query(100, le=500)):
        items = await book.get()
        if sport:
            items = [f for f in items if f["sport"] == sport]
        if q:
            nq = matcher.norm(q)
            items = [f for f in items if nq in matcher.norm(f"{f['home']} {f['away']} {f.get('tournament') or ''}")]
        return {"demo": not paid_on, "count": len(items), "fixtures": items[:limit]}

    @app.get("/agent/v1/edges", tags=["agent"])
    async def agent_edges(min_ev: float = 0.0):
        d = await markets_fn(category="sports", status="", detail=True, limit=200)
        rows = [m for m in d["markets"] if m.get("edge") and max(m["edge"]["yes"]["ev"], m["edge"]["no"]["ev"]) >= min_ev]
        rows.sort(key=lambda m: -max(m["edge"]["yes"]["ev"], m["edge"]["no"]["ev"]))
        return {"demo": not paid_on, "count": len(rows), "markets": rows}
