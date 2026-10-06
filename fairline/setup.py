"""First-run setup from the local UI: create/log in to a Panta API account, mint a key, store keys in .env.

Credentials are typed by the user into their own local Fairline instance and sent only to Panta's official auth
endpoints. Passwords are never stored or logged; only the resulting API key is written to .env. These routes answer
to localhost only."""
from __future__ import annotations

import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import config

ENV_PATH = config.ROOT / ".env"
LOCAL = {"127.0.0.1", "::1", "localhost"}


def set_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def _mask(s: str) -> str:
    """Key type only (e.g. 'pk_live_') plus the last 4 characters: never any of the secret's leading characters."""
    if not s:
        return ""
    prefix = "_".join(s.split("_")[:2]) + "_" if s.count("_") >= 2 else s.split("_")[0] + "_" if "_" in s else ""
    return f"{prefix}…{s[-4:]}" if len(s) > 12 else "set"


class PantaSignup(BaseModel):
    email: str
    password: str
    name: str | None = None


class KeyIn(BaseModel):
    apiKey: str


def mount(app: FastAPI, panta, tape) -> None:
    def local_only(req: Request) -> None:
        # Behind a proxy the client address comes from X-Forwarded-For, which a caller controls: refuse any
        # forwarded request, and refuse cross-site requests from other pages open in the same browser.
        if req.headers.get("x-forwarded-for") or req.headers.get("forwarded"):
            raise HTTPException(403, "setup is only available from this computer")
        if (req.client.host if req.client else "") not in LOCAL:
            raise HTTPException(403, "setup is only available from this computer")
        origin = req.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != req.headers.get("host", ""):
            raise HTTPException(403, "cross-site setup requests are not allowed")

    @app.get("/api/setup")
    async def setup_status(request: Request):
        local_only(request)
        out = {"panta": bool(panta.api_key), "pantaKey": _mask(panta.api_key), "solami": bool(config.SOLAMI_API_KEY),
               "solamiKey": _mask(config.SOLAMI_API_KEY), "account": None}
        if panta.api_key:
            try:
                me = await panta.whoami()
                out["account"] = {"email": me.get("email"), "canCreateMarkets": me.get("canCreateMarkets")}
            except Exception as e:  # noqa: BLE001
                out["accountError"] = str(e)[:160]
        return out

    @app.post("/api/setup/panta")
    async def setup_panta(b: PantaSignup, request: Request):
        local_only(request)
        if len(b.password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        base = config.PANTA_API_BASE
        async with httpx.AsyncClient(timeout=30) as h:
            body = {"email": b.email.strip(), "password": b.password}
            if b.name:
                body["name"] = b.name.strip()
            r = await h.post(f"{base}/auth/register/", json=body)
            created = r.status_code in (200, 201)
            if r.status_code == 409:  # account exists -> log in with the same credentials
                r = await h.post(f"{base}/auth/token/", json={"email": b.email.strip(), "password": b.password})
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code >= 400 or not d.get("access"):
                msg = d.get("message") or d.get("code") or f"HTTP {r.status_code}"
                if d.get("fields"):
                    msg += ": " + "; ".join(f"{k}: {' '.join(v)}" for k, v in d["fields"].items())
                raise HTTPException(400, f"Panta sign-up failed: {msg}")
            auth = {"Authorization": f"Bearer {d['access']}"}
            secret, env_used = None, None
            for env in ("live", "test"):
                k = await h.post(f"{base}/account/keys/", json={"env": env, "name": "fairline"}, headers=auth)
                if k.status_code in (200, 201) and k.json().get("secret"):
                    secret, env_used = k.json()["secret"], env
                    break
            if not secret:
                raise HTTPException(400, f"Panta account ok, but key creation failed: {k.text[:160]}")
        set_env("PANTA_API_KEY", secret)
        config.PANTA_API_KEY = secret
        panta.api_key = secret
        return {"ok": True, "created": created, "env": env_used, "key": _mask(secret)}

    @app.post("/api/setup/solami")
    async def setup_solami(b: KeyIn, request: Request):
        local_only(request)
        key = b.apiKey.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", key):
            raise HTTPException(400, "That does not look like a Solami API key")
        rpc = f"https://rpc.solami.dev/sol?api_key={key}"
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"})
        if r.status_code != 200 or "result" not in r.text:
            raise HTTPException(400, f"Solami rejected the key (HTTP {r.status_code})")
        set_env("SOLAMI_API_KEY", key)
        config.SOLAMI_API_KEY = key
        tape.reconfigure(rpc, f"wss://ws.solami.dev/ws/sol?api_key={key}", "Solami")
        return {"ok": True, "slot": r.json().get("result"), "key": _mask(key)}
