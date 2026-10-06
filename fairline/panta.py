"""Thin async client for the Panta public API (https://docs.panta.market).

Panta never holds keys: every write is quote -> build (unsigned tx / instructions) -> wallet signs -> broadcast ->
confirm. Fairline keeps the API key server-side and the user's wallet signs in the browser.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import config


class PantaError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class PantaClient:
    def __init__(self, api_key: str | None = None, base: str | None = None, user_id: str | None = None):
        self.api_key = api_key if api_key is not None else config.PANTA_API_KEY
        self.base = (base or config.PANTA_API_BASE).rstrip("/")
        self.user_id = user_id if user_id is not None else config.PANTA_USER_ID
        self._http = httpx.AsyncClient(timeout=30, headers={"accept": "application/json"})
        self._cache: dict[str, tuple[float, Any]] = {}
        self.program_ids: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                      attribute: bool = False) -> Any:
        if not self.api_key:
            raise PantaError(401, "NOT_CONFIGURED", "Set PANTA_API_KEY in .env to enable Panta features")
        if not path.endswith("/"):
            path += "/"  # Panta requires trailing slashes
        headers = {"X-Api-Key": self.api_key}
        if attribute and self.user_id:
            headers["X-User-Id"] = self.user_id
        for attempt in range(3):
            r = await self._http.request(method, self.base + path, params=params, json=json, headers=headers)
            if r.status_code == 429 and attempt < 2:
                await asyncio.sleep(min(float(r.headers.get("Retry-After") or 2), 10))
                continue
            break
        try:
            body = r.json()
        except ValueError:
            body = {"code": "BAD_RESPONSE", "message": r.text[:200]}
        if r.status_code >= 400:
            extra = {k: v for k, v in body.items() if k in ("field", "fields")} if isinstance(body, dict) else {}
            raise PantaError(r.status_code, body.get("code", "HTTP_%d" % r.status_code) if isinstance(body, dict) else "HTTP",
                             body.get("message", "") if isinstance(body, dict) else str(body), extra)
        return body

    async def _cached(self, key: str, ttl: float, fn):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        val = await fn()
        self._cache[key] = (time.monotonic(), val)
        return val

    # ---- reads -------------------------------------------------------------------------------------------------
    async def whoami(self) -> dict:
        return await self.request("GET", "/whoami/")

    async def categories(self) -> list[str]:
        d = await self._cached("categories", 3600, lambda: self.request("GET", "/categories/"))
        return d.get("categories") or []

    async def markets(self, category: str | None = None, status: str | None = None, max_pages: int = 10) -> list[dict]:
        async def load():
            items, cursor = [], None
            for _ in range(max_pages):
                params = {"limit": 50}
                if category:
                    params["category"] = category
                if status:
                    params["status"] = status
                if cursor:
                    params["cursor"] = cursor
                d = await self.request("GET", "/markets/", params=params)
                items += d.get("items") or []
                cursor = d.get("nextCursor")
                if not cursor:
                    break
            return items
        return await self._cached(f"markets:{category}:{status}", 30, load)

    async def market(self, market_id: str) -> dict:
        return await self._cached(f"market:{market_id}", 60, lambda: self.request("GET", f"/markets/{market_id}/"))

    async def market_trades(self, market_id: str, limit: int = 50) -> list[dict]:
        d = await self.request("GET", f"/markets/{market_id}/trades/", params={"limit": limit})
        return d.get("items") or []

    async def positions(self, wallet: str) -> list[dict]:
        d = await self.request("GET", "/positions/", params={"wallet": wallet})
        return d.get("positions") or []

    # ---- primary buy -------------------------------------------------------------------------------------------
    async def buy_quote(self, wallet: str, market_id: str, side: str, amount_usdc: str) -> dict:
        body = {"wallet": wallet, "marketId": market_id, "side": side, "amountUsdc": str(amount_usdc)}
        if self.user_id:
            body["userId"] = self.user_id
        return await self.request("POST", "/primaryorderquote/", json=body, attribute=True)

    async def buy_build(self, quote_id: str, wallet: str, max_slippage_bps: int = 100) -> dict:
        body = {"quoteId": quote_id, "wallet": wallet, "maxSlippageBps": int(max_slippage_bps)}
        if self.user_id:
            body["userId"] = self.user_id
        d = await self.request("POST", "/primaryorderbuild/", json=body, attribute=True)
        self._learn_programs(d)
        return d

    async def buy_submit(self, order_id: str, signature: str, wallet: str) -> dict:
        return await self.request("POST", "/primaryordersubmit/",
                                  json={"orderId": order_id, "signature": signature, "wallet": wallet})

    async def buy_verify(self, order_id: str, signature: str | None, wallet: str) -> dict:
        body = {"orderId": order_id, "wallet": wallet}
        if signature:
            body["signature"] = signature
        return await self.request("POST", "/primaryorderverify/", json=body)

    # ---- create market -----------------------------------------------------------------------------------------
    async def image_upload_signature(self) -> dict:
        return await self.request("POST", "/markets/create/image-upload/", json={})

    async def create_quote(self, payload: dict) -> dict:
        return await self.request("POST", "/markets/create/quote/", json=payload, attribute=True)

    async def create_build(self, create_id: str, wallet: str) -> dict:
        d = await self.request("POST", "/markets/create/build/", json={"createId": create_id, "wallet": wallet},
                               attribute=True)
        return d

    async def create_register(self, create_id: str, signature: str) -> dict:
        return await self.request("POST", "/markets/register/", json={"createId": create_id, "signature": signature})

    # ---- claims & attribution ----------------------------------------------------------------------------------
    async def claim_build(self, wallet: str, market_id: str) -> dict:
        d = await self.request("POST", "/claim/build/", json={"wallet": wallet, "marketId": market_id})
        self._learn_programs(d)
        return d

    async def report_trade(self, signature: str, wallet: str, market_id: str, quote_id: str | None = None,
                           client_order_id: str | None = None) -> dict:
        body = {"signature": signature, "wallet": wallet, "marketId": market_id}
        if quote_id:
            body["quoteId"] = quote_id
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if self.user_id:
            body["userId"] = self.user_id
        return await self.request("POST", "/trades/", json=body, attribute=True)

    def _learn_programs(self, built: dict) -> None:
        for ix in built.get("instructions") or []:
            pid = ix.get("programId")
            if pid and pid not in _SYSTEM_PROGRAMS:
                self.program_ids.add(pid)


_SYSTEM_PROGRAMS = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
}


def price(m: dict, side: str = "yes") -> float | None:
    """Best available spot price for a side (0..1). Secondary (CLOB) price wins once a market has graduated; a
    secondary side that has never traded reports 0 and falls through to the spot/primary price."""
    sec, spot, prim = f"secondary{side.title()}Price", f"{side}Price", f"primary{side.title()}Price"
    keys = (sec, spot, prim) if m.get("phase") == "secondary" else (spot, prim, sec)
    for k in keys:
        v = m.get(k)
        try:
            f = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            continue
        if f > 1:  # secondary prices are reported on the program's 1e9 fixed-point scale
            f /= 1e9
        if 0 < f <= 1:
            return min(f, 0.999999)
    return None
