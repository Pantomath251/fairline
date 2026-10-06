/* Fairline front-end: plain JS, @solana/web3.js (IIFE) and the Phantom provider. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p, d = 1) => (p == null || isNaN(p) ? "–" : (p * 100).toFixed(d) + "%");
const short = (a) => (a ? a.slice(0, 4) + "…" + a.slice(-4) : "");
const ts = (s) => new Date(s * 1000).toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const ago = (s) => { const d = Math.max(0, Date.now() / 1000 - s); return d < 60 ? `${d | 0}s ago` : d < 3600 ? `${(d / 60) | 0}m ago` : d < 86400 ? `${(d / 3600) | 0}h ago` : `${(d / 86400) | 0}d ago`; };

const state = { cfg: null, wallet: null, fixtures: [], selected: null };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const e = d.error || {}; throw new Error(e.code ? `${e.code}: ${e.message}` : d.detail || `HTTP ${r.status}`); }
  return d;
}
function toast(msg, ms = 4000) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms); }
function modal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal-close").onclick = closeModal;
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };

/* ---------- tabs ---------- */
$$(".tabs button").forEach((b) => (b.onclick = () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  ({ setup: loadSetup, markets: loadMarkets, create: loadFixtures, portfolio: loadPortfolio, tape: startTape, agents: loadAgents }[b.dataset.tab] || (() => {}))();
}));
function showTab(name) { $(`.tabs button[data-tab="${name}"]`).click(); }

/* ---------- setup ---------- */
function setPill(el, ok, text) { el.className = "pill " + (ok ? "ok" : "off"); el.textContent = text; }
async function loadSetup() {
  try {
    const s = await api("/api/setup");
    setPill($("#s-panta"), s.panta, s.panta ? `connected ${s.account?.email ? "· " + s.account.email : ""}` : "not connected");
    setPill($("#s-solami"), s.solami, s.solami ? "connected" : "not connected");
    setPill($("#s-wallet"), !!state.wallet, state.wallet ? short(state.wallet) : "not connected");
    if (s.accountError) $("#s-panta-out").textContent = s.accountError;
  } catch (e) { $("#s-panta-out").textContent = e.message; }
}
$("#s-panta-go").onclick = async () => {
  const out = $("#s-panta-out"), btn = $("#s-panta-go");
  btn.disabled = true; out.textContent = "Creating your Panta account and API key…";
  try {
    const d = await api("/api/setup/panta", { method: "POST", body: { email: $("#s-email").value, password: $("#s-pass").value } });
    $("#s-pass").value = "";
    out.textContent = `${d.created ? "Account created" : "Logged in"} · ${d.env} API key saved (${d.key}).`;
    await loadConfig(); await loadSetup();
  } catch (e) { out.textContent = e.message; } finally { btn.disabled = false; }
};
$("#s-solami-go").onclick = async () => {
  const out = $("#s-solami-out");
  out.textContent = "Checking key with Solami…";
  try {
    const d = await api("/api/setup/solami", { method: "POST", body: { apiKey: $("#s-solami-key").value } });
    $("#s-solami-key").value = "";
    out.textContent = `Saved (${d.key}) · Solana slot ${d.slot}. The live tape now streams through Solami.`;
    await loadConfig(); await loadSetup();
  } catch (e) { out.textContent = e.message; }
};

/* ---------- wallet & transactions ---------- */
const provider = () => window.phantom?.solana || (window.solana?.isPhantom ? window.solana : null);
async function connect() {
  const p = provider();
  if (!p) { window.open("https://phantom.com/download", "_blank"); return; }
  const res = await p.connect();
  state.wallet = res.publicKey.toString();
  $("#connect").textContent = short(state.wallet);
  if ($("#tab-portfolio").classList.contains("active")) loadPortfolio();
  if ($("#tab-setup").classList.contains("active")) loadSetup();
}
$("#connect").onclick = () => connect().catch((e) => toast(e.message));
provider()?.connect?.({ onlyIfTrusted: true }).then((r) => { state.wallet = r.publicKey.toString(); $("#connect").textContent = short(state.wallet); }).catch(() => {});
// Watch-only mode (?wallet=<address>): quote and build with a public address, stop before signing.
const watchOnly = new URLSearchParams(location.search).get("wallet");
if (watchOnly && !provider()) { state.wallet = watchOnly; state.watchOnly = true; $("#connect").textContent = "👁 " + short(watchOnly); }

function b64ToBytes(b64) { const s = atob(b64); const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; }
function bytesToB64(u) { let s = ""; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000)); return btoa(s); }

function txFromInstructions(build) {
  const { PublicKey, TransactionInstruction, TransactionMessage, VersionedTransaction } = solanaWeb3;
  const ixs = build.instructions.map((ix) => new TransactionInstruction({
    programId: new PublicKey(ix.programId),
    keys: ix.accounts.map((a) => ({ pubkey: new PublicKey(a.pubkey), isSigner: !!a.isSigner, isWritable: !!a.isWritable })),
    data: b64ToBytes(ix.data),
  }));
  const msg = new TransactionMessage({ payerKey: new PublicKey(state.wallet), recentBlockhash: build.recentBlockhash, instructions: ixs }).compileToV0Message();
  return new VersionedTransaction(msg);
}

/** Wallet signs; Fairline relays through Solami RPC and waits for confirmation. */
async function signAndSend(tx) {
  if (!provider()) throw new Error("Unsigned transaction ready: open Fairline in a browser with Phantom to sign it");
  const signed = await provider().signTransaction(tx);
  const { signature } = await api("/api/rpc/send", { method: "POST", body: { transaction: bytesToB64(signed.serialize()) } });
  for (let i = 0; i < 40; i++) {
    let status = null;
    try { ({ status } = await api("/api/rpc/status", { method: "POST", body: { signature } })); } catch (_) { /* transient: keep polling */ }
    if (status?.err) throw new Error("Transaction failed on-chain: " + JSON.stringify(status.err));
    if (status && ["confirmed", "finalized"].includes(status.confirmationStatus)) return signature;
    await sleep(1500);
  }
  throw new Error(`Not confirmed after 60s (signature ${signature}); check Solscan before retrying`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Retry a Panta call while the chain catches up (Panta verifies at its own commitment level). */
async function retryWhile(fn, pattern, tries = 10, wait = 3000) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) { if (!pattern.test(e.message) || i >= tries - 1) throw e; await sleep(wait); }
  }
}

function steps(list) { return `<div class="steps">${list.map((s, i) => `<div class="step" id="st-${i}">○ ${esc(s)}</div>`).join("")}</div>`; }
/** Mark the first unfinished step as failed — or as paused when a watch-only session stops before signing. */
function failStep(out, e) {
  const cur = $$(".step", out).find((s) => !s.classList.contains("done"));
  if (!cur) return toast(e.message);
  const paused = /^Unsigned transaction ready/.test(e.message);
  cur.className = "step" + (paused ? "" : " err");
  cur.textContent = (paused ? "⏸ " : "✗ ") + e.message;
}
function stepState(i, cls, text) { const el = $("#st-" + i); if (!el) return; el.className = "step " + cls; el.textContent = (cls === "done" ? "✓ " : cls === "err" ? "✗ " : "… ") + text; }

/* ---------- status pills ---------- */
async function loadConfig() {
  state.cfg = await api("/api/config");
  const pp = $("#pill-panta"); pp.className = "pill " + (state.cfg.pantaConfigured ? "ok" : "off"); pp.textContent = state.cfg.pantaConfigured ? "Panta API ✓" : "Panta API key missing";
  const ps = $("#pill-solami"); ps.className = "pill " + (state.cfg.solamiConfigured ? "ok" : ""); ps.textContent = state.cfg.solamiConfigured ? "Solami stream ✓" : "Public RPC";
}

/* ---------- price box ---------- */
$("#price-go").onclick = async () => {
  const q = $("#price-q").value.trim(); if (!q) return;
  $("#price-out").textContent = "pricing…";
  try {
    const d = await api("/api/price?question=" + encodeURIComponent(q));
    $("#price-out").innerHTML = d.matched
      ? `<b>${esc(d.label)}</b> · fair YES <b class="edge pos">${pct(d.fairYes)}</b> (${d.fixture.home} v ${d.fixture.away}, ${ts(d.fixture.startTimestamp)})`
      : "No upcoming fixture with odds matches that question.";
  } catch (e) { $("#price-out").textContent = e.message; }
};
$("#price-q").onkeydown = (e) => { if (e.key === "Enter") $("#price-go").click(); };

/* ---------- markets ---------- */
async function loadMarkets() {
  const box = $("#markets");
  if (!state.cfg?.pantaConfigured) { box.innerHTML = `<div class="empty">Connect a Panta API account in <a href="#" onclick="showTab('setup');return false">Setup</a> to load Panta markets.</div>`; return; }
  box.innerHTML = `<div class="empty">Loading Panta markets…</div>`;
  try {
    const cat = $("#m-category").value; const open = $("#m-open").checked;
    const d = await api(`/api/markets?category=${cat}`);
    let rows = d.markets;
    if (open) rows = rows.filter((m) => ["primary", "secondary"].includes(m.phase));
    rows.sort((a, b) => (b.edge ? Math.max(b.edge.yes.ev, b.edge.no.ev) : -9) - (a.edge ? Math.max(a.edge.yes.ev, a.edge.no.ev) : -9));
    const openCount = d.markets.filter((m) => ["primary", "secondary"].includes(m.phase)).length;
    $("#m-meta").textContent = `${d.markets.length} Panta markets · ${openCount} open · ${rows.filter((m) => m.fair).length} matched to fixtures`;
    box.innerHTML = rows.length ? rows.map(marketCard).join("") : `<div class="empty">No Panta market is open for an upcoming match right now.<br><br>
      <button class="btn primary" onclick="showTab('create')">List one from ${state.cfg.fixtures || "upcoming"} fixtures</button>
      <button class="btn ghost" onclick="$('#m-open').checked=false;loadMarkets()">Show closed markets</button></div>`;
    $$("[data-buy]", box).forEach((b) => (b.onclick = () => buyFlow(rows.find((m) => m.marketId === b.dataset.id), b.dataset.buy)));
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function marketCard(m) {
  const yes = m.yes;
  const fair = m.fair?.fairYes;
  const e = m.edge;
  const best = e ? e[e.bestSide] : null;
  return `<div class="card">
    <div class="meta"><span class="tag">${esc(m.category || "")}</span><span class="tag">${esc(m.phase || "")}</span>${m.onchain?.isResolved ? `<span class="tag">resolved ${m.onchain.yesWins ? "YES" : "NO"}</span>` : ""}${m.startTime ? `<span>${ts(m.startTime)}</span>` : ""}${e?.value ? `<span class="tag value">value on ${e.bestSide.toUpperCase()}</span>` : ""}</div>
    <h3>${esc(m.title || m.marketId)}</h3>${m.titleSource === "chain" ? `<div class="meta">question read from the market's on-chain account via Solami</div>` : ""}
    ${m.fair ? `<div class="meta">Fairline reads: <b>${esc(m.fair.label)}</b> · ${esc(m.fixture.home)} v ${esc(m.fixture.away)}</div>` : `<div class="meta">No fixture match (non-sports or past event)</div>`}
    <div class="bars">
      <span>Market YES</span><div class="bar"><i style="width:${(yes || 0) * 100}%"></i></div><span>${yes != null ? pct(+yes) : "–"}</span>
      <span>Fair YES</span><div class="bar fair"><i style="width:${(fair || 0) * 100}%"></i></div><span>${pct(fair)}</span>
    </div>
    ${best ? `<div class="row">EV per $1 on ${e.bestSide.toUpperCase()}: <span class="edge ${best.ev > 0 ? "pos" : "neg"}">${(best.ev * 100).toFixed(1)}%</span><span class="muted">· ¼-Kelly ${pct(best.kelly / 4)}</span></div>` : ""}
    <div class="row">${m.phase === "primary" ? `<button class="btn yes" data-buy="yes" data-id="${m.marketId}">Buy YES</button><button class="btn no" data-buy="no" data-id="${m.marketId}">Buy NO</button>` : `<span class="muted">${m.phase === "secondary" ? "Secondary (order book) phase" : "Closed"}</span>`}</div>
  </div>`;
}
$("#m-refresh").onclick = loadMarkets; $("#m-category").onchange = loadMarkets; $("#m-open").onchange = loadMarkets;

async function buyFlow(m, side) {
  if (!state.wallet) await connect();
  if (!state.wallet) return;
  const fair = m.fair ? (side === "yes" ? m.fair.fairYes : 1 - m.fair.fairYes) : null;
  modal(`<h2>Buy ${side.toUpperCase()}</h2><p>${esc(m.title)}</p>
    ${fair != null ? `<p class="muted">Fair ${side.toUpperCase()} probability: <b>${pct(fair)}</b> (${esc(m.fair.label)})</p>` : ""}
    <div class="row"><input id="amt" value="5" size="8"> USDC <button class="btn primary" id="q">Get quote</button></div>
    <div id="qout"></div>`);
  $("#q").onclick = async () => {
    const out = $("#qout");
    try {
      out.innerHTML = steps(["Quote from Panta", "Build transaction", "Sign in Phantom", "Send via Solami RPC", "Confirm with Panta"]);
      stepState(0, "", "Quoting…");
      const q = await api("/api/buy/quote", { method: "POST", body: { wallet: state.wallet, marketId: m.marketId, side, amountUsdc: $("#amt").value } });
      const avg = +q.avgPrice;
      stepState(0, "done", `${q.shares} shares @ avg ${avg.toFixed(4)} · fee ${q.feeUsdc} USDC${fair != null ? ` · edge vs fair ${((fair - avg) * 100).toFixed(1)} pts` : ""}`);
      stepState(1, "", "Building…");
      const b = await api("/api/buy/build", { method: "POST", body: { quoteId: q.quoteId, wallet: state.wallet, maxSlippageBps: 100 } });
      stepState(1, "done", `Order ${b.orderId} · expected ${b.expectedShares} shares`);
      stepState(2, "", "Waiting for Phantom…");
      const sig = await signAndSend(txFromInstructions(b));
      stepState(2, "done", "Signed"); stepState(3, "done", `Confirmed: ${short(sig)}`);
      out.insertAdjacentHTML("beforeend", `<p><a target="_blank" href="https://solscan.io/tx/${sig}">View on Solscan</a></p>`);
      // The buy is final on-chain now: attribute it first (signature-keyed, no session needed), then sync the order.
      api("/api/trades/report", { method: "POST", body: { signature: sig, wallet: state.wallet, marketId: m.marketId, quoteId: q.quoteId, orderId: b.orderId } }).catch(() => {});
      stepState(4, "", "Submitting…");
      try {
        await api("/api/buy/submit", { method: "POST", body: { orderId: b.orderId, signature: sig, wallet: state.wallet } });
        const v = await api("/api/buy/verify", { method: "POST", body: { orderId: b.orderId, signature: sig, wallet: state.wallet } });
        stepState(4, "done", `Panta status: ${v.status}`);
      } catch (e) { stepState(4, "done", `Bought on-chain; Panta order sync pending (${e.message})`); }
    } catch (e) { failStep(out, e); }
  };
}

/* ---------- fixtures / create ---------- */
async function loadFixtures() {
  if (state.fixtures.length) return renderFixtures();
  $("#fixtures").innerHTML = `<div class="empty">Loading fixtures with odds…</div>`;
  const d = await api("/api/fixtures?limit=1000");
  state.fixtures = d.fixtures;
  renderFixtures();
}
function renderFixtures() {
  const sport = $("#f-sport").value, q = $("#f-q").value.toLowerCase(), order = $("#f-order").value;
  const soon = Date.now() / 1000 + 3720;
  const rows = state.fixtures.filter((f) => f.startTimestamp > soon && (!sport || f.sport === sport) && (!q || `${f.home} ${f.away} ${f.tournament}`.toLowerCase().includes(q)));
  if (order === "top") rows.sort((a, b) => b.priority - a.priority || a.startTimestamp - b.startTimestamp);
  $("#f-meta").textContent = `${rows.length} upcoming fixtures with odds`;
  $("#fixtures").innerHTML = rows.slice(0, 300).map((f) => `<div class="fx" data-id="${f.id}">
    <div><div class="teams">${esc(f.home)} <span class="muted">v</span> ${esc(f.away)}</div><div class="meta">${esc(f.sport)} · ${esc(f.tournament || "")} · ${ts(f.startTimestamp)}</div></div>
    <div class="odds"><span>1 ${pct(f.fair.home, 0)}</span>${f.fair.draw != null ? `<span>X ${pct(f.fair.draw, 0)}</span>` : ""}<span>2 ${pct(f.fair.away, 0)}</span></div></div>`).join("") || `<div class="empty">No fixtures.</div>`;
  $$(".fx").forEach((el) => (el.onclick = () => selectFixture(+el.dataset.id, el)));
}
$("#f-sport").onchange = renderFixtures; $("#f-q").oninput = renderFixtures; $("#f-order").onchange = renderFixtures;

async function selectFixture(id, el) {
  $$(".fx").forEach((x) => x.classList.toggle("sel", x === el));
  const d = await api("/api/fixtures/" + id);
  const f = d.fixture;
  state.selected = f;
  $("#fixture-detail").innerHTML = `<h3>${esc(f.home)} v ${esc(f.away)}</h3>
    <div class="meta">${esc(f.tournament || "")} · ${ts(f.startTimestamp)} · bookmaker overround ${pct(f.fair.overround)}</div>
    ${f.model ? `<div class="meta">Expected goals ${f.model.lambdaHome} – ${f.model.lambdaAway} · over 2.5 ${pct(f.model.over25)} · BTTS ${pct(f.model.btts)}</div>` : ""}
    <div class="props">${d.propositions.map((p) => `<div class="prop"><span>${esc(p.question)}</span><b>${pct(p.fairYes)}</b><button class="btn" data-kind="${p.kind}">Preview</button></div>`).join("")}</div>
    <div id="create-box"></div>`;
  $$("[data-kind]", $("#fixture-detail")).forEach((b) => (b.onclick = () => previewCreate(f, b.dataset.kind)));
}

async function previewCreate(f, kind) {
  const box = $("#create-box");
  box.innerHTML = `<p class="muted">Preparing…</p>`;
  try {
    const d = await api(`/api/create/preview?fixtureId=${f.id}&kind=${kind}`);
    const p = d.payload;
    box.innerHTML = `<img class="cover" src="${d.image}" alt="market cover">
      <div class="kv"><div>Question</div><div>${esc(p.question)}</div><div>Resolution</div><div>${esc(p.resolutionRule)}</div>
      <div>Sources</div><div>${p.sourcesOfTruth.map((s) => `<a href="${esc(s)}" target="_blank">${esc(s.replace(/^https?:\/\//, "").slice(0, 40))}</a>`).join("<br>")}</div>
      <div>Trading closes</div><div>${ts(p.startTime)}</div><div>Resolves after</div><div>${ts(p.resolutionTime)}</div><div>Fair YES</div><div><b>${pct(d.fairYes)}</b></div></div>
      ${d.warning ? `<p class="step err">${esc(d.warning)}</p>` : ""}
      <button class="btn primary" id="do-create" ${d.warning || !state.cfg?.pantaConfigured ? "disabled" : ""}>Create on Panta</button>
      <span class="muted">${state.cfg?.pantaConfigured ? "You pay Panta’s creation fee from your wallet." : "Needs PANTA_API_KEY on the server."}</span>
      <div id="create-steps"></div>`;
    $("#do-create").onclick = () => createFlow(f, kind);
  } catch (e) { box.innerHTML = `<p class="step err">${esc(e.message)}</p>`; }
}

async function createFlow(f, kind) {
  if (!state.wallet) await connect();
  if (!state.wallet) return;
  const out = $("#create-steps");
  out.innerHTML = steps(["Upload cover + quote fee (Panta)", "Build create transaction", "Sign in Phantom", "Send via Solami RPC", "Register market (Panta)"]);
  try {
    stepState(0, "", "Uploading & quoting…");
    // Panta's create endpoints occasionally answer "unexpected ... failure"; a short retry usually succeeds.
    const q = await retryWhile(() => api("/api/create/quote", { method: "POST", body: { fixtureId: f.id, kind, wallet: state.wallet } }), /unexpected/i, 3, 2500);
    stepState(0, "done", `Fee ${(+q.paymentUsdc / 1e6).toFixed(2)} USDC (liquidity ${(+q.liquidityInjectionUsdc / 1e6).toFixed(2)}) · market ${short(q.expectedEventPda)}`);
    stepState(1, "", "Building…");
    const b = await retryWhile(() => api("/api/create/build", { method: "POST", body: { createId: q.createId, wallet: state.wallet } }), /unexpected/i, 3, 2500);
    stepState(1, "done", "Unsigned transaction ready");
    stepState(2, "", "Waiting for Phantom…");
    const tx = solanaWeb3.VersionedTransaction.deserialize(b64ToBytes(b.transaction));
    const sig = await signAndSend(tx);
    stepState(2, "done", "Signed"); stepState(3, "done", `Confirmed: ${short(sig)}`);
    stepState(4, "", "Registering…");
    const register = () => api("/api/create/register", { method: "POST", body: { createId: q.createId, signature: sig } });
    try {
      // Panta verifies at its own commitment; registration is idempotent for the same createId + signature.
      const r = await retryWhile(register, /TX_NOT_FOUND/);
      stepState(4, "done", `Live on Panta: ${short(r.marketId)}`);
    } catch (e) {
      stepState(4, "err", `Created on-chain, registration pending: ${e.message}`);
      out.insertAdjacentHTML("beforeend", `<button class="btn" id="retry-reg">Retry registration</button>`);
      $("#retry-reg").onclick = async () => {
        try { const r = await register(); stepState(4, "done", `Live on Panta: ${short(r.marketId)}`); $("#retry-reg").remove(); }
        catch (err) { toast(err.message, 7000); }
      };
    }
  } catch (e) { failStep(out, e); }
}

/* ---------- portfolio ---------- */
async function loadPortfolio() {
  const box = $("#portfolio");
  if (!state.wallet) { box.innerHTML = `<p class="muted">Connect Phantom to load positions.</p><button class="btn primary" onclick="connect()">Connect</button>`; return; }
  box.innerHTML = `<p class="muted">Loading ${short(state.wallet)}…</p>`;
  try {
    const d = await api("/api/positions?wallet=" + state.wallet);
    if (!d.positions.length) { box.innerHTML = `<p class="muted">No Panta positions for ${short(state.wallet)} yet.</p>`; return; }
    box.innerHTML = `<p>Estimated value: <b>${d.estTotalUsdc.toFixed(2)} USDC</b></p><table><tr><th>Market</th><th>Side</th><th>Shares</th><th>Phase</th><th>Est. value</th><th></th></tr>
      ${d.positions.map((p) => `<tr><td>${esc(p.title || short(p.marketId))}</td><td>${p.side.toUpperCase()}</td><td>${p.shares}</td><td>${p.phase}${p.outcome ? " · " + p.outcome.toUpperCase() + " won" : ""}</td><td>${p.estValueUsdc.toFixed(2)}</td>
      <td>${p.claimable && !p.claimed ? `<button class="btn primary" data-claim="${p.marketId}">Claim</button>` : p.claimed ? "claimed" : ""}</td></tr>`).join("")}</table>`;
    $$("[data-claim]", box).forEach((b) => (b.onclick = () => claimFlow(b.dataset.claim, b)));
  } catch (e) { box.innerHTML = `<p class="step err">${esc(e.message)}</p>`; }
}
async function claimFlow(marketId, btn) {
  btn.disabled = true; btn.textContent = "Building…";
  try {
    const b = await api("/api/claim/build", { method: "POST", body: { wallet: state.wallet, marketId } });
    btn.textContent = "Sign in Phantom…";
    const sig = await signAndSend(txFromInstructions(b));
    api("/api/trades/report", { method: "POST", body: { signature: sig, wallet: state.wallet, marketId } }).catch(() => {});
    btn.textContent = "Claimed ✓"; toast("Claimed " + b.winningShares + " shares");
  } catch (e) { btn.disabled = false; btn.textContent = "Claim"; toast(e.message, 7000); }
}

/* ---------- live tape ---------- */
let es = null;
function txRow(it, isNew) {
  const amt = it.usdc != null ? `${it.usdc > 0 ? "+" : ""}${it.usdc.toFixed(2)} USDC` : it.sol != null && Math.abs(it.sol) > 0.001 ? `${it.sol > 0 ? "+" : ""}${it.sol.toFixed(3)} SOL` : "";
  return `<div class="tx${isNew ? " new" : ""}"><span class="muted">${ago(it.time)}</span><span class="lbl">${esc(it.label)}${it.quote ? ` <span class="tag">${it.quote}</span>` : ""}</span>
    <span>${esc(it.question || (it.market ? "market " + short(it.market) : ""))} ${it.wallet ? `<span class="muted">· ${short(it.wallet)}</span>` : ""} ${amt ? `<b>${amt}</b>` : ""}</span>
    <a href="https://solscan.io/tx/${it.signature}" target="_blank"><code>${short(it.signature)}</code></a></div>`;
}
async function startTape() {
  const d = await api("/api/tape?limit=100");
  renderTapeStatus(d.status);
  $("#tape").innerHTML = d.items.map((i) => txRow(i)).join("") || `<div class="empty">Waiting for Panta transactions…</div>`;
  if (es) return;
  es = new EventSource("/api/tape/stream");
  es.onmessage = (e) => { const it = JSON.parse(e.data); $(".empty", $("#tape"))?.remove(); $("#tape").insertAdjacentHTML("afterbegin", txRow(it, true)); };
  es.addEventListener("status", (e) => renderTapeStatus(JSON.parse(e.data)));
}
function renderTapeStatus(s) {
  $("#tape-status").innerHTML = `${s.connected ? "● live" : "○ connecting"} via <b>${esc(s.provider)}</b>${s.mode ? ` (${esc(s.mode)})` : ""} · ${s.messages} new transactions since start${s.lastSlot ? " · slot " + s.lastSlot : ""}${s.error ? ` · <span class="step err">${esc(s.error)}</span>` : ""}`;
}

/* ---------- agents ---------- */
async function loadAgents() {
  const d = await api("/agent");
  const base = location.origin;
  $("#agents").innerHTML = `<p>Payments: <b>${d.payment.enabled ? `x402 · USDC on ${esc(d.payment.network)}` : "demo mode (free)"}</b></p>
    <table><tr><th>Endpoint</th><th>Price</th><th>What it returns</th></tr>${d.endpoints.map((e) => `<tr><td><code>${e.method} ${esc(e.path.replace(base, ""))}</code></td><td>${e.price}</td><td>${esc(e.description)}</td></tr>`).join("")}</table>
    <h3>Try it</h3><pre class="code">curl "${base}/agent/v1/price?question=Will%20Arsenal%20beat%20Chelsea%20by%202%20or%20more%20goals%3F"</pre>
    <p class="muted">Any x402 client (Coinbase x402 SDKs, AgentKit, x402-fetch, MCP x402 tools) pays automatically after the 402 response.</p>`;
}

/* ---------- boot ---------- */
loadConfig().then(() => (state.cfg.pantaConfigured && state.cfg.solamiConfigured ? loadMarkets() : showTab("setup"))).catch((e) => toast(e.message));
