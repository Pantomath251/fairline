"""Fairline: bookmaker-grade fair odds, one-click market creation and trading for Panta prediction markets.

Run:  uvicorn app:app --port 8000   (configure .env first, see .env.example)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fairline import cards, config, marketgen, matcher, odds
from fairline.fixtures import FixtureBook
from fairline.panta import PantaClient, PantaError, price as panta_price
from fairline.tape import Tape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("fairline")

STATIC = Path(__file__).resolve().parent / "static"
FEE_BPS = float(config.env("PANTA_PRIMARY_FEE_BPS", "200"))

book = FixtureBook()
panta = PantaClient()
tape = Tape()
_bg: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _bg.append(asyncio.create_task(book.run_forever()))
    if config.env("TAPE", "1") != "0":
        _bg.append(asyncio.create_task(tape.run_forever()))
    yield
    for t in _bg:
        t.cancel()
    await panta.close()


app = FastAPI(title="Fairline", version="1.0.0", lifespan=lifespan,
              description="Fair odds, market creation and trading for Panta prediction markets on Solana. "
                          "Powered by Panta.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"])


@app.exception_handler(PantaError)
async def _panta_error(_req: Request, e: PantaError):
    return JSONResponse(status_code=e.status if 400 <= e.status < 600 else 502, content={"error": e.as_dict()})


@app.exception_handler(ValueError)
async def _value_error(_req: Request, e: ValueError):
    return JSONResponse(status_code=400, content={"error": {"code": "BAD_REQUEST", "message": str(e)}})


# ------------------------------------------------------------------------------------------------------------------
# Meta
# ------------------------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config():
    return {**config.public_config(), "programIds": tape.program_ids, "feeBps": FEE_BPS,
            "fixturesUpdated": int(book.updated), "fixtures": len(book.items)}


@app.get("/api/health")
async def health():
    return {"ok": True, "fixtures": len(book.items), "tape": tape.status}


# ------------------------------------------------------------------------------------------------------------------
# Fixtures & fair odds
# ------------------------------------------------------------------------------------------------------------------
@app.get("/api/fixtures")
async def fixtures(sport: str = "", q: str = "", limit: int = Query(200, le=1000)):
    items = await book.get()
    if sport:
        items = [f for f in items if f["sport"] == sport]
    if q:
        nq = matcher.norm(q)
        items = [f for f in items if nq in matcher.norm(f"{f['home']} {f['away']} {f.get('tournament') or ''}")]
    return {"updated": int(book.updated), "count": len(items), "fixtures": items[:limit]}


@app.get("/api/fixtures/{fixture_id}")
async def fixture(fixture_id: int):
    await book.get()
    fx = book.by_id.get(fixture_id)
    if not fx:
        raise HTTPException(404, "fixture not found or already started")
    props = [{"kind": k, "question": marketgen.TEMPLATES[k].format(home=fx["home"], away=fx["away"]),
              "fairYes": round(marketgen.fair_yes(fx, k) or 0, 4)} for k in marketgen.kinds_for(fx)]
    return {"fixture": fx, "propositions": props}


@app.get("/api/price")
async def price_question(question: str, start: int = 0):
    """Price a free-text prediction-market question about an upcoming match."""
    m = matcher.match_market({"title": question, "startTime": start}, await book.get())
    if not m:
        return {"matched": False}
    return {"matched": True, "label": m["label"], "fairYes": m["fairYes"], "proposition": m["proposition"],
            "fixture": m["fixture"]}


# ------------------------------------------------------------------------------------------------------------------
# Panta markets with fair value
# ------------------------------------------------------------------------------------------------------------------
async def _enrich_market(m: dict, fixtures_list: list[dict], detail: bool) -> dict:
    out = dict(m)
    if detail:
        # list rows can lag (empty titles, stale phase); the detail row is authoritative and carries spot prices
        try:
            d = await panta.market(m["marketId"])
            out.update({k: v for k, v in d.items() if v not in (None, "")})
        except PantaError as e:
            out["priceError"] = e.code
        if not out.get("title"):
            # Many catalog rows have no title; the question is always on-chain in the market's Event account.
            try:
                acct = await tape.market_account(m["marketId"])
            except Exception as e:  # noqa: BLE001
                acct = None
                log.debug("on-chain read %s failed: %s", m["marketId"], e)
            if acct and acct.get("question"):
                out.update(title=acct["question"], titleSource="chain", onchain=acct)
    out["yes"], out["no"] = panta_price(out, "yes"), panta_price(out, "no")
    closed = (out.get("endTime") or 0) and out["endTime"] < time.time() - 3600
    match = None if closed or out.get("category") not in (None, "", "sports") else matcher.match_market(out, fixtures_list)
    if match:
        out["fair"] = {k: match[k] for k in ("fixtureId", "label", "fairYes", "proposition")}
        out["fixture"] = {k: match["fixture"][k] for k in ("id", "home", "away", "tournament", "startTime", "sport")}
        yes = panta_price(out, "yes")
        if yes:
            out["edge"] = odds.edge(match["fairYes"], yes, FEE_BPS)
    return out


@app.get("/api/markets")
async def markets(category: str = "", status: str = "", detail: bool = True, limit: int = Query(100, le=500)):
    rows = await panta.markets(category or None, status or None)
    rows = sorted(rows, key=lambda m: (m.get("phase") not in ("primary", "secondary"), -(m.get("startTime") or 0)))[:limit]
    fx = await book.get()
    sem = asyncio.Semaphore(6)

    async def one(m):
        async with sem:
            return await _enrich_market(m, fx, detail)
    enriched = await asyncio.gather(*(one(m) for m in rows))
    return {"count": len(enriched), "markets": enriched}


@app.get("/api/markets/{market_id}")
async def market(market_id: str):
    m = await panta.market(market_id)
    out = await _enrich_market(m, await book.get(), detail=False)
    try:
        out["trades"] = await panta.market_trades(market_id, 50)
    except PantaError:
        out["trades"] = []
    return out


@app.get("/api/positions")
async def positions(wallet: str):
    rows = await panta.positions(wallet)
    prices: dict[str, dict] = {}
    for mid in {r["marketId"] for r in rows}:
        try:
            prices[mid] = await panta.market(mid)
        except PantaError:
            prices[mid] = {}
    total = 0.0
    for r in rows:
        m = prices.get(r["marketId"]) or {}
        r["title"] = m.get("title")
        shares = float(r.get("shares") or 0)
        if r.get("outcome"):
            value = shares if r["outcome"] == r["side"] else 0.0
        else:
            value = shares * (panta_price(m, r["side"]) or 0.0)
        r["estValueUsdc"] = round(value, 4)
        total += value
    return {"wallet": wallet, "positions": rows, "estTotalUsdc": round(total, 4)}


# ------------------------------------------------------------------------------------------------------------------
# Trading: quote -> build (Panta) -> sign (wallet, browser) -> send (Solami RPC) -> submit/verify (Panta)
# ------------------------------------------------------------------------------------------------------------------
class BuyQuote(BaseModel):
    wallet: str
    marketId: str
    side: str = Field(pattern="^(yes|no|YES|NO)$")
    amountUsdc: str


class BuyBuild(BaseModel):
    quoteId: str
    wallet: str
    maxSlippageBps: int = 100


class OrderSig(BaseModel):
    orderId: str
    wallet: str
    signature: str | None = None


class ClaimReq(BaseModel):
    wallet: str
    marketId: str


class ReportReq(BaseModel):
    signature: str
    wallet: str
    marketId: str
    quoteId: str | None = None
    orderId: str | None = None


@app.post("/api/buy/quote")
async def buy_quote(b: BuyQuote):
    return await panta.buy_quote(b.wallet, b.marketId, b.side.lower(), b.amountUsdc)


@app.post("/api/buy/build")
async def buy_build(b: BuyBuild):
    d = await panta.buy_build(b.quoteId, b.wallet, b.maxSlippageBps)
    for pid in panta.program_ids:
        tape.add_program(pid)
    return d


@app.post("/api/buy/submit")
async def buy_submit(b: OrderSig):
    if not b.signature:
        raise ValueError("signature required")
    return await panta.buy_submit(b.orderId, b.signature, b.wallet)


@app.post("/api/buy/verify")
async def buy_verify(b: OrderSig):
    return await panta.buy_verify(b.orderId, b.signature, b.wallet)


@app.post("/api/trades/report")
async def trades_report(b: ReportReq):
    return await panta.report_trade(b.signature, b.wallet, b.marketId, b.quoteId, b.orderId)


@app.post("/api/claim/build")
async def claim_build(b: ClaimReq):
    return await panta.claim_build(b.wallet, b.marketId)


# ------------------------------------------------------------------------------------------------------------------
# Market creation from a fixture
# ------------------------------------------------------------------------------------------------------------------
class CreateReq(BaseModel):
    fixtureId: int
    kind: str
    wallet: str


class CreateBuild(BaseModel):
    createId: str
    wallet: str


class CreateRegister(BaseModel):
    createId: str
    signature: str


async def _fixture_or_404(fid: int) -> dict:
    await book.get()
    fx = book.by_id.get(fid)
    if not fx:
        raise HTTPException(404, "fixture not found or already started")
    return fx


@app.get("/api/cards/{fixture_id}/{kind}.png")
async def card(fixture_id: int, kind: str):
    fx = await _fixture_or_404(fixture_id)
    q = marketgen.TEMPLATES[kind].format(home=fx["home"], away=fx["away"])
    png = await asyncio.to_thread(cards.render, fx, q, marketgen.fair_yes(fx, kind))
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/create/preview")
async def create_preview(fixtureId: int, kind: str):
    fx = await _fixture_or_404(fixtureId)
    payload = marketgen.build(fx, kind, strict=False)
    warning = "Kick-off is less than an hour away: Panta needs startTime at least 3600s ahead, so this one can only " \
              "be previewed." if marketgen.too_soon(fx) else None
    return {"payload": payload, "fairYes": marketgen.fair_yes(fx, kind), "image": f"/api/cards/{fixtureId}/{kind}.png",
            "fixture": fx, "warning": warning}


async def _upload_card(fx: dict, kind: str) -> str:
    sig = await panta.image_upload_signature()
    q = marketgen.TEMPLATES[kind].format(home=fx["home"], away=fx["away"])
    png = await asyncio.to_thread(cards.render, fx, q, marketgen.fair_yes(fx, kind))
    async with httpx.AsyncClient(timeout=60) as h:
        r = await h.post(sig["uploadUrl"], data={k: str(v) for k, v in (sig.get("fields") or {}).items()},
                         files={"file": (f"fairline-{fx['id']}-{kind}.png", png, "image/png")})
    if r.status_code >= 400:
        raise PantaError(502, "IMAGE_UPLOAD_FAILED", r.text[:200])
    return r.json()["secure_url"]


@app.post("/api/create/quote")
async def create_quote(b: CreateReq):
    fx = await _fixture_or_404(b.fixtureId)
    image_url = await _upload_card(fx, b.kind)
    payload = marketgen.build(fx, b.kind, image_url)
    payload["wallet"] = b.wallet
    q = await panta.create_quote(payload)
    return {**q, "payload": payload}


@app.post("/api/create/build")
async def create_build(b: CreateBuild):
    return await panta.create_build(b.createId, b.wallet)


@app.post("/api/create/register")
async def create_register(b: CreateRegister):
    return await panta.create_register(b.createId, b.signature)


# ------------------------------------------------------------------------------------------------------------------
# Solana relay (signed transactions are broadcast through Solami; the browser never sees the RPC key)
# ------------------------------------------------------------------------------------------------------------------
class SendReq(BaseModel):
    transaction: str  # base64 signed VersionedTransaction


class SigReq(BaseModel):
    signature: str


@app.post("/api/rpc/send")
async def rpc_send(b: SendReq):
    base64.b64decode(b.transaction, validate=True)
    try:
        sig = await tape.rpc("sendTransaction", [b.transaction, {"encoding": "base64", "skipPreflight": False,
                                                                 "preflightCommitment": "confirmed", "maxRetries": 5}])
    except RuntimeError as e:
        raise HTTPException(400, str(e)[:500])
    return {"signature": sig, "provider": tape.status["provider"]}


@app.post("/api/rpc/status")
async def rpc_status(b: SigReq):
    res = await tape.rpc("getSignatureStatuses", [[b.signature], {"searchTransactionHistory": True}])
    return {"status": (res or {}).get("value", [None])[0]}


# ------------------------------------------------------------------------------------------------------------------
# Live on-chain tape (Solami)
# ------------------------------------------------------------------------------------------------------------------
@app.get("/api/tape")
async def tape_recent(limit: int = Query(100, le=300)):
    return {"status": tape.status, "programIds": tape.program_ids, "items": list(tape.items)[:limit]}


@app.get("/api/tape/stream")
async def tape_stream(request: Request):
    q = tape.subscribe()

    async def gen():
        try:
            yield f"event: status\ndata: {json.dumps(tape.status)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {item}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: status\ndata: {json.dumps(tape.status)}\n\n"
        finally:
            tape.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------------------------
# Agent API (x402 pay-per-call for AI agents)
# ------------------------------------------------------------------------------------------------------------------
from fairline import agent, setup  # noqa: E402  (needs `book`, `panta` and `tape` defined above)

agent.mount(app, book, lambda **kw: markets(**kw))
setup.mount(app, panta, tape)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
