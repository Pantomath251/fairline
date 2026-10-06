# Fairline: fair odds for prediction markets

**Is this market priced right?** Fairline reads every Panta sports market, finds the real fixture it is about, and
prices it against de-vigged odds from Pinnacle, the sharpest and lowest-margin sportsbook. When a market disagrees with the fair probability, you can buy the
mispriced side in two clicks with Phantom. It can also turn tomorrow's fixtures into well-specified Panta markets in
one click, stream Panta activity live from Solana through Solami, and serve the same fair odds to AI agents over x402.

> Powered by Panta · built for the Colosseum Crypto World's Fair (Panta API and Solami sidetracks)

## Why

Prediction markets on long-tail sports questions are thin, and nobody tells a trader whether 45¢ for
"Will Arsenal beat Chelsea by 2+ goals?" is cheap. Sportsbooks already price these matches with deep liquidity.
Fairline uses those prices as a public, explainable reference so that:

- **traders** see the edge before they buy;
- **creators** launch markets with clean resolution rules and a sensible fair price, for any fixture, in seconds;
- **agents** can ask "what is this question really worth?" for a fraction of a cent.

## Features

| | What it does | Panta / Solana pieces used |
|---|---|---|
| **Markets** | Lists Panta markets with spot prices, matches each sports question to its fixture, shows fair YES, edge, EV per $1 and a ¼-Kelly hint | `GET /markets/`, `GET /markets/{id}/` |
| **Price any question** | Free-text pricing: win, draw, win by N+, exact score, totals, both teams to score, early goals, clean sheets, "not lose" | Poisson goal model fitted to 1X2 |
| **Buy** | Quote, build, sign in Phantom, broadcast through Solami RPC, then submit, verify and report for attribution | `/primaryorderquote/` `/primaryorderbuild/` `/primaryordersubmit/` `/primaryorderverify/` `/trades/` |
| **Create from fixtures** | ~950 upcoming fixtures with odds. Picks question, resolution rule, sources and times, renders a 1024×1024 cover, uploads it and builds the create transaction | `/markets/create/image-upload/` `/markets/create/quote/` `/markets/create/build/` `/markets/register/` |
| **Portfolio** | Positions marked to market; one-click win claims | `/positions/` `/claim/build/` |
| **Live on-chain** | `logsSubscribe` on the Panta program over Solami WebSocket RPC, with Anchor events decoded from the program IDL (creations, buys, resolutions, claims) and USDC flows from `getTransaction` | Solami RPC + WebSocket |
| **Agent API** | `/agent/v1/price`, `/agent/v1/fixtures` and `/agent/v1/edges`, paid per call with x402 (USDC) | x402 + Bazaar discovery |

## How the pricing works

1. **Sharp odds → fair probabilities.** Pinnacle moneylines (1X2 for football) and main totals are pulled for ~1,000
   upcoming matches across football, basketball, ice hockey and tennis (SofaScore consensus odds are the fallback).
   They are de-vigged with the power method (`p_i^k`, with `Σ = 1`), which removes more margin from longshots than
   proportional normalisation does. Pinnacle's margin is about 2.5%, so the fair prices are tight.
2. **Goal model.** For football, Fairline fits independent Poisson rates `λ_home` and `λ_away` to the fair home/away
   win probabilities and the fair probability of going over the main goal line. That one model then prices any
   goal-based proposition consistently: margins, exact scores, totals, BTTS, clean sheets, and "scores in the first
   N minutes" (rate × N/90).
3. **Question reading.** Team names, aliases and nicknames ("Spurs", "Man Utd", "PSG"…) are located in the market
   text, which is templated (`@H` / `@A`) and classified by a rule set. Markets are matched to a fixture only within
   the market's own time window.
4. **Edge.** For price `q` and fair probability `p`, the expected profit per $1 after Panta's primary fee `f` is
   `p(1−f)/q − 1`. Kelly is `(p(1−f) − q)/((1−f) − q)`; the UI shows a quarter of it.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # add PANTA_API_KEY and SOLAMI_API_KEY
uvicorn app:app --port 8000
# open http://localhost:8000 and connect Phantom
python -m pytest -q          # unit tests
```

| Variable | Purpose |
|---|---|
| `PANTA_API_KEY` | Panta public API key (`pk_test_…` / `pk_live_…`). Stays on the server; the browser never sees it |
| `PANTA_USER_ID` | Attribution id sent as `X-User-Id` |
| `SOLAMI_API_KEY` | Solami RPC + WebSocket, used for the live tape and for broadcasting signed transactions |
| `PAY_TO`, `X402_NETWORK`, `PUBLIC_URL` | Optional x402 payments for the agent API |

Without keys the app still runs: fixtures, fair odds, question pricing, cover previews, and the live tape over the
public RPC.

## Architecture

```
browser (vanilla JS + @solana/web3.js + Phantom)
   │  sign only: keys never leave the wallet
FastAPI (app.py)
   ├─ fairline/panta.py      Panta API client (quote → build → submit/verify, create, claims, attribution)
   ├─ fairline/fixtures.py   SofaScore fixtures + odds → fair probabilities (cached, refreshed in background)
   ├─ fairline/goals.py      Poisson goal model fitted to 1X2
   ├─ fairline/matcher.py    reads market questions → fixture + proposition → fair YES
   ├─ fairline/marketgen.py  fixture → Panta create payload; cards.py renders the 1024² cover
   ├─ fairline/anchor.py     generic Anchor IDL decoder (events + instructions, Borsh)
   ├─ fairline/tape.py       Solami logsSubscribe → decode → SSE to browsers
   └─ fairline/agent.py      x402 pay-per-call agent endpoints
```

## Notes and disclosures

- Odds come from Pinnacle's public guest API (the same reads pinnacle.com makes for anonymous visitors) and from
  SofaScore's public match data as a fallback. They are used as a reference price, not as betting advice.
- Panta's catalog has no title for many markets, so Fairline reads the question straight from each market's
  on-chain `Event` account via Solami RPC and decodes it with the program IDL.
- On Solami's free plan WebSocket access is not included, so the live tape automatically falls back to polling
  `getSignaturesForAddress` every 2 seconds on Solami RPC. With a WebSocket-enabled plan it uses `logsSubscribe`.
- Fairline never takes custody: every Panta write is an unsigned transaction that the user signs in their own
  wallet.
- `fairline/sofascore.py` was written by the same author in early October 2026 (inside the hackathon window) for an
  earlier data project and reused here. Everything else was built for this submission.
- The Panta program IDL in `fairline/idl/` was taken from Panta's public web app and is used only to decode public
  on-chain events.

## License

MIT
