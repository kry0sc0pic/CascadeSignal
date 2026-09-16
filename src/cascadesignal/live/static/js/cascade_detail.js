// Cascade detail + replay view. Renders each section as its own function
// appended to #panels -- add a panel by writing one function and adding it
// to the `renderers` list in `render()`, per the "keep it modular, panels
// will be added" ask. All values here come straight from
// /api/cascades/{protocol}/{cascade_id} -- nothing computed client-side
// beyond formatting and simple deltas (duration, elapsed-since-start).

let REPLAY = null; // { events: [...], index: 0, timer: null }

function panel(title, bodyHtml) {
  const el = document.createElement("div");
  el.className = "panel";
  el.innerHTML = `<h2>${title}</h2>${bodyHtml}`;
  return el;
}

// -- summary -----------------------------------------------------------

function renderSummary(data) {
  const c = data.cascade;
  const durS = (c.start_time && c.end_time)
    ? (new Date(c.end_time) - new Date(c.start_time)) / 1000
    : null;
  return panel("Cascade summary", `
    <div class="stat-row">
      <div class="stat"><div class="label">Status</div><div class="value">${c.status}</div></div>
      <div class="stat"><div class="label">Block range</div><div class="value">${fmtBlock(c.start_block)}–${fmtBlock(c.end_block)}</div></div>
      <div class="stat"><div class="label">Start time</div><div class="value">${fmtTime(c.start_time)}</div></div>
      <div class="stat"><div class="label">End time</div><div class="value">${fmtTime(c.end_time)}</div></div>
      <div class="stat"><div class="label">Duration</div><div class="value">${fmtDuration(durS)}</div></div>
      <div class="stat"><div class="label">Liquidations in cluster</div><div class="value">${c.n_liquidations}</div></div>
      <div class="stat"><div class="label">Cluster gap / min size</div><div class="value">${c.cluster_gap_blocks} blocks / ${c.min_cluster_size}</div></div>
      <div class="stat"><div class="label">Alarm threshold (n(t))</div><div class="value">${fmtNum(data.threshold)}</div></div>
    </div>
  `);
}

// -- timing: cluster start vs. algorithm firing, raw values only -------

function renderTiming(data) {
  const c = data.cascade;
  const horizon = c.early_warning_horizon_blocks;

  if (c.early_warned) {
    return panel("Timing — early warning", `
      <div class="stat-row">
        <div class="stat"><div class="label">Warned at</div><div class="value">block ${fmtBlock(c.first_alert_block)}</div></div>
        <div class="stat"><div class="label">Cascade started</div><div class="value">block ${fmtBlock(c.start_block)}<br>${fmtTime(c.start_time)}</div></div>
        <div class="stat"><div class="label">Lead time</div><div class="value" style="color:var(--green)">${c.lead_blocks} blocks<br>${fmtDuration(c.lead_seconds)}</div></div>
      </div>
      <div class="empty-note">Alarm crossed the threshold ${c.lead_blocks} block(s) before the first liquidation of this cascade, inside the ${horizon}-block early-warning horizon. Lead is measured in blocks — the alert's own wall-clock stamp (${fmtTime(c.first_alert_time)}) is when scoring ran, which for replayed history is not the block time.</div>
    `);
  }
  if (c.alert_fired) {
    return panel("Timing — fired, but not in advance", `
      <div class="stat-row">
        <div class="stat"><div class="label">Cascade started</div><div class="value">block ${fmtBlock(c.start_block)}<br>${fmtTime(c.start_time)}</div></div>
        <div class="stat"><div class="label">Algorithm first fired</div><div class="value">block ${fmtBlock(c.first_alert_block)}</div></div>
        <div class="stat"><div class="label">Lead time</div><div class="value" style="color:var(--amber)">none</div></div>
      </div>
      <div class="empty-note">The alarm fired only after the cascade was already underway — reactive, not an early warning. No crossing occurred in the ${horizon} blocks before onset.</div>
    `);
  }
  return panel("Timing — no alert", `
    <div class="empty-note">The alarm never crossed the threshold, in the ${horizon} blocks before onset or during the cascade. Raw fact, not a miss/hit judgment — read the signal sequence below to see how close n(t) got.</div>
  `);
}

// -- raw model state (constant per bar unless refit occurred) ----------

function renderModelState(bars) {
  if (!bars.length) return panel("Model state", `<div class="empty-note">No bars in window.</div>`);
  const b = bars[bars.length - 1];
  return panel("Model state (fitted params, as of last bar in window)", `
    <div class="stat-row">
      <div class="stat"><div class="label">mu</div><div class="value">${fmtNum(b.mu, 6)}</div></div>
      <div class="stat"><div class="label">alpha</div><div class="value">${fmtNum(b.alpha)}</div></div>
      <div class="stat"><div class="label">beta</div><div class="value">${fmtNum(b.beta)}</div></div>
    </div>
  `);
}

// -- signal-in-sequence table --------------------------------------------

function renderSignalSequence(bars) {
  if (!bars.length) {
    return panel("Algorithm signal, in sequence", `<div class="empty-note">No scored bars in this window.</div>`);
  }
  const rows = bars.map((b, i) => `
    <tr data-bar-idx="${i}">
      <td>${fmtBlock(b.start_block)}–${fmtBlock(b.end_block)}</td>
      <td>${fmtTime(b.scored_at)}</td>
      <td>${b.n_liquidations}</td>
      <td>${fmtNum(b.n_t)}</td>
      <td>${fmtNum(b.r)}</td>
      <td>${fmtNum(b.lam, 6)}</td>
    </tr>`).join("");
  return panel("Algorithm signal, in sequence", `
    <div class="scroll-table">
      <table>
        <tr><th>blocks</th><th>scored at</th><th>liquidations</th><th>n(t)</th><th>r (self-excited history)</th><th>lambda (intensity)</th></tr>
        ${rows}
      </table>
    </div>
  `);
}

// -- raw on-chain inputs ---------------------------------------------------

function renderLiquidations(liqs) {
  if (!liqs.length) {
    return panel("Raw on-chain inputs (confirmed liquidations)", `<div class="empty-note">None in this window.</div>`);
  }
  const rows = liqs.map((r, i) => `
    <tr data-liq-idx="${i}">
      <td>${fmtBlock(r.block_number)}</td>
      <td>${fmtTime(r.block_timestamp)}</td>
      <td>${shortHex(r.tx_hash)}</td>
      <td>${shortHex(r.user)}</td>
      <td>${shortHex(r.collateral_asset)}</td>
      <td>${shortHex(r.debt_asset)}</td>
      <td>${shortHex(r.liquidator)}</td>
    </tr>`).join("");
  return panel("Raw on-chain inputs (confirmed liquidations)", `
    <div class="scroll-table">
      <table>
        <tr><th>block</th><th>time</th><th>tx</th><th>user</th><th>collateral</th><th>debt</th><th>liquidator</th></tr>
        ${rows}
      </table>
    </div>
  `);
}

// -- replay ---------------------------------------------------------------

function buildReplayEvents(bars, liqs) {
  const events = [
    ...liqs.map((r, i) => ({ kind: "liquidation", block: r.block_number, liqIdx: i })),
    ...bars.map((b, i) => ({ kind: "bar", block: b.end_block, barIdx: i })),
  ];
  events.sort((a, b) => a.block - b.block || (a.kind === "liquidation" ? -1 : 1));
  return events;
}

function applyReplayHighlight() {
  if (!REPLAY) return;
  document.querySelectorAll("[data-bar-idx]").forEach(el => el.classList.remove("replay-current", "replay-past"));
  document.querySelectorAll("[data-liq-idx]").forEach(el => el.classList.remove("replay-current", "replay-past"));
  let lastNt = null, lastLiqCount = 0;
  for (let i = 0; i <= REPLAY.index && i < REPLAY.events.length; i++) {
    const ev = REPLAY.events[i];
    const sel = ev.kind === "bar" ? `[data-bar-idx="${ev.barIdx}"]` : `[data-liq-idx="${ev.liqIdx}"]`;
    const el = document.querySelector(sel);
    if (!el) continue;
    el.classList.add(i === REPLAY.index ? "replay-current" : "replay-past");
    if (ev.kind === "bar") lastNt = REPLAY.bars[ev.barIdx].n_t;
    if (ev.kind === "liquidation") lastLiqCount++;
  }
  const label = document.getElementById("replay-step-label");
  if (label) {
    label.textContent = `${REPLAY.index + 1} / ${REPLAY.events.length}` +
      (lastNt != null ? ` — n(t)=${fmtNum(lastNt)}` : "") +
      ` — ${lastLiqCount} liquidation(s) so far`;
  }
  const slider = document.getElementById("replay-slider");
  if (slider) slider.value = REPLAY.index;
}

function replayStep(delta) {
  if (!REPLAY) return;
  REPLAY.index = Math.max(0, Math.min(REPLAY.events.length - 1, REPLAY.index + delta));
  applyReplayHighlight();
}

function replayPlay() {
  if (!REPLAY || REPLAY.timer) return;
  REPLAY.timer = setInterval(() => {
    if (REPLAY.index >= REPLAY.events.length - 1) {
      clearInterval(REPLAY.timer);
      REPLAY.timer = null;
      return;
    }
    replayStep(1);
  }, 400);
}

function replayPause() {
  if (REPLAY && REPLAY.timer) {
    clearInterval(REPLAY.timer);
    REPLAY.timer = null;
  }
}

function renderReplayControls(bars, liqs) {
  const events = buildReplayEvents(bars, liqs);
  REPLAY = { events, bars, index: events.length ? 0 : -1, timer: null };
  if (!events.length) {
    return panel("Replay", `<div class="empty-note">Nothing to replay in this window.</div>`);
  }
  return panel("Replay (steps through the sequence above)", `
    <div class="replay-controls">
      <button id="replay-reset">⏮</button>
      <button id="replay-back">◀</button>
      <button id="replay-play">▶</button>
      <button id="replay-pause">⏸</button>
      <button id="replay-fwd">▶▶</button>
      <input type="range" id="replay-slider" min="0" max="${events.length - 1}" value="0">
      <span class="step-label" id="replay-step-label"></span>
    </div>
  `);
}

function wireReplayControls() {
  const byId = (id) => document.getElementById(id);
  byId("replay-reset")?.addEventListener("click", () => { REPLAY.index = 0; applyReplayHighlight(); });
  byId("replay-back")?.addEventListener("click", () => replayStep(-1));
  byId("replay-fwd")?.addEventListener("click", () => replayStep(1));
  byId("replay-play")?.addEventListener("click", replayPlay);
  byId("replay-pause")?.addEventListener("click", replayPause);
  byId("replay-slider")?.addEventListener("input", (e) => {
    REPLAY.index = Number(e.target.value);
    applyReplayHighlight();
  });
}

// -- top-level load ---------------------------------------------------------

async function loadCascadeDetail(protocol, id) {
  const panelsEl = document.getElementById("panels");
  if (!protocol || !id) {
    panelsEl.innerHTML = `<div class="empty-note">Missing ?protocol= and ?id= in the URL.</div>`;
    return;
  }
  document.getElementById("title").textContent = `Cascade ${id}`;
  document.getElementById("subtitle").textContent = `Protocol: ${protocol}`;
  document.getElementById("banner").textContent =
    "UI-side liquidation-clustering heuristic (gap-based, on raw confirmed liquidation blocks) — " +
    "not the ADR-001 D-A cascade definition, and not a ground-truth episode. No accuracy, precision, " +
    "recall, or hit/miss judgment is computed anywhere on this page — the numbers below are exactly " +
    "what the algorithm produced; whether that counts as a hit is for you to judge.";

  try {
    const res = await fetch(`/api/cascades/${encodeURIComponent(protocol)}/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    panelsEl.innerHTML = "";
    const renderers = [
      () => renderSummary(data),
      () => renderTiming(data),
      () => renderReplayControls(data.bars, data.liquidations),
      () => renderSignalSequence(data.bars),
      () => renderLiquidations(data.liquidations),
      () => renderModelState(data.bars),
    ];
    for (const r of renderers) panelsEl.appendChild(r());
    wireReplayControls();
    applyReplayHighlight();
  } catch (e) {
    panelsEl.innerHTML = `<div class="empty-note">Failed to load cascade: ${e}</div>`;
  }
}
