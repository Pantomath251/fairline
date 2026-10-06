"""Runtime configuration, read once from the environment (and an optional .env file next to app.py)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


PANTA_API_BASE = env("PANTA_API_BASE", "https://live-api.panta.market/api/v1").rstrip("/")
PANTA_API_KEY = env("PANTA_API_KEY")
# Attribution id sent as X-User-Id so Panta credits volume routed through Fairline.
PANTA_USER_ID = env("PANTA_USER_ID", "fairline")

SOLAMI_API_KEY = env("SOLAMI_API_KEY")
# Solana JSON-RPC used to relay signed transactions and read chain state. Solami when a key is set.
SOLANA_RPC_URL = env("SOLANA_RPC_URL") or (
    f"https://rpc.solami.dev/sol?api_key={SOLAMI_API_KEY}" if SOLAMI_API_KEY else "https://api.mainnet-beta.solana.com")
SOLANA_WS_URL = env("SOLANA_WS_URL") or (
    f"wss://ws.solami.dev/ws/sol?api_key={SOLAMI_API_KEY}" if SOLAMI_API_KEY else "wss://api.mainnet-beta.solana.com")

# Panta programs to watch on-chain (comma separated). The SOL-quoted market program is public in Panta's web bundle;
# the USDC program id is learned at runtime from the first instruction Panta's build endpoint returns.
PANTA_PROGRAM_IDS = [p for p in env("PANTA_PROGRAM_IDS", "6gM5afTQBq5VZCfgpGqcsqzfWd5maLSCKWtGjbEobZMp").split(",") if p]

# x402 pay-per-call agent API. Empty PAY_TO disables payments (agent routes answer for free, flagged as demo).
PAY_TO = env("PAY_TO")
X402_NETWORK = env("X402_NETWORK", "eip155:8453")
FACILITATOR_URL = env("FACILITATOR_URL", "https://facilitator.payai.network")
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")

FIXTURE_DAYS = int(env("FIXTURE_DAYS", "4"))
FIXTURE_SPORTS = [s for s in env("FIXTURE_SPORTS", "football,basketball,tennis,ice-hockey").split(",") if s]


def public_config() -> dict:
    """What the browser is allowed to know. Never includes secrets."""
    return {
        "pantaConfigured": bool(PANTA_API_KEY),
        "solamiConfigured": bool(SOLAMI_API_KEY),
        "rpcProvider": "Solami" if SOLAMI_API_KEY and not env("SOLANA_RPC_URL") else "custom" if env("SOLANA_RPC_URL") else "public",
        "agentPayments": bool(PAY_TO),
        "x402Network": X402_NETWORK,
        "programIds": PANTA_PROGRAM_IDS,
    }
