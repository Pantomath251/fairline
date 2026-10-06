"""Live on-chain tape of Panta activity, streamed from Solana through Solami.

Subscribes to `logsSubscribe` for the Panta program over Solami's WebSocket RPC, decodes the Anchor events in each
transaction, enriches them with USDC/SOL flows from `getTransaction`, and fans the result out to browser clients
(Server-Sent Events)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque

import httpx
import websockets

from . import config
from .anchor import Idl, Reader, load_all

log = logging.getLogger("fairline.tape")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LABELS = {
    "CreateEvent": "Market created", "CreateBreakingEvent": "Breaking market created", "PrimaryOrder": "Primary buy",
    "SecondaryLimitOrder": "Limit order", "CancelSecondaryOrder": "Order cancelled", "ClaimWinnings": "Winnings claimed",
    "ClaimWin": "Winnings claimed", "ResolveEvent": "Market resolved", "SubmitOracleResult": "Oracle result submitted",
    "GraduateMarket": "Market graduated", "GraduateBreakingEvent": "Market graduated",
    "ClaimCreatorFees": "Creator fees claimed", "SettleFailedMarket": "Failed market settled",
    "DisputeResolvedEvent": "Resolution disputed", "ClaimPrimaryRefund": "Refund claimed", "ClaimLamports": "Funds withdrawn",
    "ForceCancelOrder": "Order force-cancelled", "ReapFilledOrder": "Filled order closed",
}


def _base(ix: str) -> str:
    return ix[:-4] if ix.endswith("Usdc") else ix


def _redact(s: str) -> str:
    """Strip API keys from URLs in error text before it reaches logs or the browser."""
    return re.sub(r"(api_key=)[^&\s'\"]+", r"\1***", s)


class Tape:
    def __init__(self, program_ids: list[str] | None = None, rpc_url: str | None = None, ws_url: str | None = None,
                 maxlen: int = 300):
        self.program_ids = list(program_ids or config.PANTA_PROGRAM_IDS)
        self.rpc_url = rpc_url or config.SOLANA_RPC_URL
        self.ws_url = ws_url or config.SOLANA_WS_URL
        self.idls: dict[str, Idl] = load_all()
        self.items: deque[dict] = deque(maxlen=maxlen)
        self.seen: set[str] = set()
        self.subscribers: set[asyncio.Queue] = set()
        self.questions: dict[str, str] = {}
        self.status = {"connected": False, "provider": "Solami" if config.SOLAMI_API_KEY else "public RPC",
                       "mode": "WebSocket logsSubscribe", "since": None, "messages": 0, "lastSlot": None, "error": None}
        self._http = httpx.AsyncClient(timeout=30)
        self._sem = asyncio.Semaphore(4)
        self._newest: dict[str, str] = {}

    # ---- RPC ---------------------------------------------------------------------------------------------------
    async def rpc(self, method: str, params: list, retries: int = 4):
        for attempt in range(retries + 1):
            r = await self._http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            try:
                d = r.json()
            except ValueError:
                d = {"error": {"code": r.status_code, "message": r.text[:120]}}
            err = d.get("error")
            limited = r.status_code == 429 or (isinstance(err, dict) and err.get("code") in (429, -32429))
            if limited and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if err:
                raise RuntimeError(f"{method}: {err}")
            return d.get("result")

    async def get_tx(self, sig: str) -> dict | None:
        for attempt in range(4):
            tx = await self.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                         "maxSupportedTransactionVersion": 0}])
            if tx:
                return tx
            await asyncio.sleep(1.5 * (attempt + 1))
        return None

    # ---- decoding ----------------------------------------------------------------------------------------------
    def decode(self, sig: str, logs: list[str], err, slot: int | None, block_time: int | None) -> dict:
        events, ixs = [], []
        for pid in self.program_ids:
            idl = self.idls.get(pid)
            if idl:
                events += idl.decode_logs(logs)
                ixs += idl.instruction_names(logs)
        market = None
        for ev in events:
            data = ev.get("data") or {}
            market = market or data.get("event")
            if data.get("question") and data.get("event"):
                self.questions[data["event"]] = data["question"]
        primary = next((i for i in ixs if _base(i) in LABELS), ixs[0] if ixs else "Transaction")
        return {"signature": sig, "slot": slot, "time": block_time or int(time.time()), "ok": err is None,
                "instruction": primary, "label": LABELS.get(_base(primary), primary),
                "quote": "USDC" if primary.endswith("Usdc") else "SOL" if ixs else None, "instructions": ixs,
                "events": events, "market": market, "question": self.questions.get(market) if market else None}

    def _flows(self, tx: dict) -> dict:
        meta = tx.get("meta") or {}
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
        signer = next((k["pubkey"] for k in msg.get("accountKeys") or [] if isinstance(k, dict) and k.get("signer")),
                      keys[0] if keys else None)
        out = {"wallet": signer, "usdc": None, "sol": None}
        if signer:
            def bal(rows):
                return sum(float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) for b in rows or []
                           if b.get("mint") == USDC_MINT and b.get("owner") == signer)
            d_usdc = bal(meta.get("postTokenBalances")) - bal(meta.get("preTokenBalances"))
            if abs(d_usdc) > 1e-9:
                out["usdc"] = round(d_usdc, 6)
            if keys and meta.get("preBalances") and meta.get("postBalances"):
                i = keys.index(signer)
                out["sol"] = round((meta["postBalances"][i] - meta["preBalances"][i]) / 1e9, 6)
        return out

    async def _enrich_and_publish(self, item: dict) -> None:
        async with self._sem:
            try:
                tx = await self.get_tx(item["signature"])
                if tx:
                    item.update(self._flows(tx))
                    item["time"] = tx.get("blockTime") or item["time"]
                    if not item["events"] and not item["instructions"]:
                        item.update({k: v for k, v in self.decode(item["signature"], (tx.get("meta") or {}).get("logMessages") or [],
                                                                  None, tx.get("slot"), tx.get("blockTime")).items()
                                     if k in ("events", "instructions", "instruction", "label", "market", "question")})
            except Exception as e:  # noqa: BLE001
                log.debug("enrich %s failed: %s", item["signature"], e)
        self._publish(item)

    def _publish(self, item: dict) -> None:
        self.items.appendleft(item)
        payload = json.dumps(item)
        for q in list(self.subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    # ---- history -----------------------------------------------------------------------------------------------
    async def backfill(self, limit: int = 25) -> None:
        for pid in self.program_ids:
            try:
                sigs = await self.rpc("getSignaturesForAddress", [pid, {"limit": limit, "commitment": "confirmed"}])
            except Exception as e:  # noqa: BLE001
                log.warning("backfill %s failed: %s", pid, _redact(str(e)))
                self.status["error"] = _redact(str(e))[:200]
                continue
            if sigs:
                self._newest[pid] = sigs[0]["signature"]
            for s in reversed(sigs or []):
                sig = s["signature"]
                if not self._mark_seen(sig):
                    continue
                try:
                    tx = await self.get_tx(sig)
                except Exception as e:  # noqa: BLE001 - skip one bad/limited fetch, keep the rest of the history
                    log.debug("backfill tx %s: %s", sig, e)
                    continue
                if not tx:
                    continue
                if not config.SOLAMI_API_KEY:
                    await asyncio.sleep(0.4)  # stay under public RPC limits
                item = self.decode(sig, (tx.get("meta") or {}).get("logMessages") or [], s.get("err"), s.get("slot"),
                                   s.get("blockTime"))
                item.update(self._flows(tx))
                item["backfill"] = True
                self._publish(item)

    # ---- live stream -------------------------------------------------------------------------------------------
    def _mark_seen(self, sig: str) -> bool:
        """True if new. Keeps the de-dup set bounded."""
        if sig in self.seen:
            return False
        if len(self.seen) > 50_000:
            self.seen = {i["signature"] for i in self.items}
        self.seen.add(sig)
        return True

    async def poll_forever(self, interval: float = 2.0) -> None:
        """Near-real-time fallback when the RPC plan has no WebSocket access: poll getSignaturesForAddress with
        `until` = newest signature already seen, then decode each new transaction."""
        self.status.update(connected=True, mode=f"RPC polling every {interval:g}s", since=int(time.time()), error=None)
        newest: dict[str, str | None] = {pid: self._newest.get(pid) for pid in self.program_ids}
        while True:
            for pid in list(self.program_ids):
                params = {"limit": 25, "commitment": "confirmed"}
                if newest.get(pid):
                    params["until"] = newest[pid]
                try:
                    sigs = await self.rpc("getSignaturesForAddress", [pid, params], retries=2) or []
                    self.status["error"] = None
                except Exception as e:  # noqa: BLE001
                    self.status["error"] = _redact(f"{type(e).__name__}: {e}")[:160]
                    continue
                if sigs:
                    newest[pid] = sigs[0]["signature"]
                for s in reversed(sigs):
                    if not self._mark_seen(s["signature"]):
                        continue
                    self.status["messages"] += 1
                    self.status["lastSlot"] = s.get("slot")
                    item = {"signature": s["signature"], "slot": s.get("slot"), "time": s.get("blockTime") or int(time.time()),
                            "ok": s.get("err") is None, "instruction": "Transaction", "label": "Transaction", "quote": None,
                            "instructions": [], "events": [], "market": None, "question": None}
                    asyncio.create_task(self._enrich_and_publish(item))
            await asyncio.sleep(interval)

    async def run_forever(self) -> None:
        try:
            await self.backfill()
        except Exception as e:  # noqa: BLE001
            log.warning("backfill failed: %s", e)
        delay = 2
        while True:
            if self.status.get("mode", "").startswith("RPC polling"):
                await self.poll_forever()
                return
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
                    self._ws = ws
                    for i, pid in enumerate(self.program_ids):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                                  "params": [{"mentions": [pid]}, {"commitment": "confirmed"}]}))
                    self.status.update(connected=True, since=int(time.time()), error=None)
                    delay = 2
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("method") != "logsNotification":
                            continue
                        self.status["messages"] += 1
                        res = msg["params"]["result"]
                        val, slot = res["value"], res["context"]["slot"]
                        self.status["lastSlot"] = slot
                        sig = val["signature"]
                        if not self._mark_seen(sig):
                            continue
                        item = self.decode(sig, val.get("logs") or [], val.get("err"), slot, None)
                        asyncio.create_task(self._enrich_and_publish(item))
            except websockets.InvalidStatus as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (400, 401, 403, 405):
                    # e.g. Solami free tier: "WebSocket access requires a plan" -> stream by polling the same RPC
                    log.info("WebSocket not available on this plan (HTTP %s); switching to RPC polling", code)
                    self.status["mode"] = "RPC polling"
                    continue
                self.status.update(connected=False, error=f"WebSocket HTTP {code}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception as e:  # noqa: BLE001
                self.status.update(connected=False, error=_redact(f"{type(e).__name__}: {e}")[:160])
                log.warning("tape stream dropped: %s (retry in %ss)", _redact(str(e)), delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def market_account(self, market_id: str) -> dict | None:
        """Decode a Panta market's on-chain `Event` account (question, rule, sources, volumes, resolution).
        Markets are immutable where it matters (question/rule/sources), so results are cached."""
        cache = self.__dict__.setdefault("_acct_cache", {})
        if market_id in cache:
            return cache[market_id]
        info = await self.rpc("getAccountInfo", [market_id, {"encoding": "base64", "commitment": "confirmed"}])
        val = (info or {}).get("value")
        if not val or val.get("owner") not in self.idls:
            cache[market_id] = None
            return None
        idl = self.idls[val["owner"]]
        raw = base64.b64decode(val["data"][0])
        acct_names = {bytes(a["discriminator"]): a["name"] for a in idl.idl.get("accounts") or []}
        if acct_names.get(raw[:8]) != "Event":
            cache[market_id] = None
            return None
        try:
            ev = idl.read_defined(Reader(raw[8:]), "Event")
        except (ValueError, KeyError, IndexError):
            cache[market_id] = None
            return None
        out = {"question": ev.get("question"), "resolutionRule": ev.get("resolution_rule"),
               "sourcesOfTruth": ev.get("source_of_truth"), "creator": ev.get("creator"),
               "isResolved": ev.get("is_resolved"), "yesWins": ev.get("yes_wins") if ev.get("is_resolved") else None,
               "isGraduated": ev.get("is_graduated"), "totalTrades": ev.get("total_trades"),
               "marketType": ev.get("market_type")}
        if out["isResolved"]:
            cache[market_id] = out  # only cache final state; open markets keep changing
        return out

    def reconfigure(self, rpc_url: str, ws_url: str, provider: str) -> None:
        """Switch RPC/WebSocket endpoints (e.g. after a Solami key is added) and force a reconnect."""
        self.rpc_url, self.ws_url = rpc_url, ws_url
        self.status["provider"] = provider
        ws = getattr(self, "_ws", None)
        if ws is not None:
            asyncio.create_task(ws.close())

    def add_program(self, pid: str) -> None:
        if pid not in self.program_ids:
            self.program_ids.append(pid)
