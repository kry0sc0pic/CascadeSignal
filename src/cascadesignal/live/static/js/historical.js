// Historical cascades — the "results slide". Each cascade renders the Hawkes
// branching ratio n(t) overlaid on the cumulative liquidation cost, over the
// same block window. The story: n(t) crosses the alarm threshold BEFORE the
// bulk of the cost lands. Data from /api/historical (materialized offline by
// scripts/live/build_historical_cascades.py — same fit + bar clock as Live).

function fmtUsd(x, dp) {
  if (x == null) return "—";
  if (x >= 1e9) return `$${(x / 1e9).toFixed(dp ?? 1)}B`;
  if (x >= 1e6) return `$${(x / 1e6).toFixed(dp ?? 1)}M`;
  if (x >= 1e3) return `$${(x / 1e3).toFixed(0)}k`;
  return `$${x.toFixed(0)}`;
}
function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function setupCanvas(canvas) {
  const dpr = devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

// ---- list view ----------------------------------------------------------

async function renderHistoricalList(root) {
  root.innerHTML = `
    <div class="hero">
      <div class="eyebrow">Results · Ethereum mainnet · Aave v2 &amp; v3</div>
      <h1>The alarm rises before the cost lands.</h1>
      <div class="lede">Each major liquidation cascade, with the Hawkes
      branching ratio <b>n(t)</b> replayed over it. In every case n(t) crosses
      the alarm threshold <b>before the cascade begins</b> (advance warning),
      and well before half its dollar cost is realized — the <b>same operating
      point as the Live tab</b> (fit, threshold, and debounce), resolved
      separately per protocol.</div>
    </div>
    <div id="grid" class="grid"><div class="empty-note">Loading cascades…</div></div>
    <footer>Historical n(t) is the current fit applied to past data (“what the alarm as configured would have shown”). Read-only.</footer>`;
  const grid = root.querySelector("#grid");
  try {
    const res = await fetch("/api/historical");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cascades = await res.json();
    if (!cascades.length) {
      grid.innerHTML = `<div class="empty-note">No historical cascades precomputed. Run scripts/live/build_historical_cascades.py.</div>`;
      return;
    }
    grid.innerHTML = cascades.map(shortcutCard).join("");
    cascades.forEach((c) => {
      const cv = grid.querySelector(`canvas[data-id="${cssEsc(c.id)}"]`);
      if (cv) drawMiniOverlay(cv, c.preview, c.threshold);
    });
  } catch (e) {
    grid.innerHTML = `<div class="empty-note">Failed to load: ${e}</div>`;
  }
}
function cssEsc(s) { return s.replace(/"/g, '\\"'); }

function shortcutCard(c) {
  const href = `historical.html?id=${encodeURIComponent(c.id)}`;
  // ADR-006: show both — advance warning (before cascade start) as the headline,
  // and the alarm -> 50%-of-USD figure alongside it, each labelled.
  const adv = c.lead ? `${c.lead.advance_warning_minutes} min` : null;
  const half = c.lead && c.lead.lead_minutes != null ? `${c.lead.lead_minutes} min` : null;
  const go = c.lead
    ? `<b>${adv}</b> before onset${half ? ` · ${half} to ½ cost` : ""}`
    : "no threshold crossing";
  return `
    <a class="shortcut" href="${href}">
      <div class="name">${c.name} <span class="proto-tag">${c.protocol}</span></div>
      <div class="date">${fmtTime(c.start_time)}</div>
      <div class="spark-wrap"><canvas data-id="${c.id}"></canvas></div>
      <div class="metric-row">
        <div class="metric"><div class="label">Liquidated</div><div class="value cost">${fmtUsd(c.window_cost_usd)}</div></div>
        <div class="metric"><div class="label">Peak n(t)</div><div class="value nt">${fmtNum(c.peak_n_t, 3)}</div></div>
        <div class="metric"><div class="label">Before onset</div><div class="value">${adv ?? "—"}</div></div>
      </div>
      <span class="go">${go} →</span>
    </a>`;
}

// ---- detail view --------------------------------------------------------

async function renderHistoricalDetail(root, id) {
  root.innerHTML = `<div class="empty-note">Loading timeline…</div>`;
  let d;
  try {
    const res = await fetch(`/api/historical/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    d = await res.json();
  } catch (e) {
    root.innerHTML = `<a class="back" href="historical.html">← back</a><div class="empty-note">Failed to load ${id}: ${e}</div>`;
    return;
  }

  const model = analyze(d);
  const advTxt = model.advanceMin != null ? `${model.advanceMin} min` : "—";
  root.innerHTML = `
    <div class="hero">
      <a class="back" href="historical.html">← all cascades</a>
      <div class="eyebrow" style="margin-top:14px">${fmtTime(d.start_time)} · ${d.protocol}${d.debounce_k > 1 ? " · debounce k=" + d.debounce_k : ""}</div>
      <h1>${d.name}</h1>
      <div class="lede">${d.blurb || ""}</div>
    </div>
    ${model.crossBlock != null ? `<div class="kpi">
      <div class="big">${advTxt}</div>
      <div class="cap">of <b>advance warning</b> — the alarm crosses its threshold this
        long before the cascade begins (first liquidation).${model.lead ? ` It then reaches
        <b>50% of the ${fmtUsd(model.total)}</b> liquidated <b>${model.lead.lead_minutes} min</b>
        after firing (alarm → ½ cost).` : ""} n(t) peaks at <b>${fmtNum(model.peak, 4)}</b>.</div>
    </div>` : ""}
    <div class="panel">
      <div class="panel-head">
        <h2>Branching ratio n(t) vs. cumulative liquidations</h2>
        <div class="controls">
          <div class="seg" id="metric-toggle">
            <button data-metric="cost" class="active">Cost ($)</button>
            <button data-metric="volume">Volume (#)</button>
          </div>
          <div class="seg seg-nt" id="nt-toggle">
            <button data-zoom="full" class="active">n(t) 0–1</button>
            <button data-zoom="zoom">Zoom</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart"></canvas></div>
      <div class="legend">
        <span class="l-cost" id="leg-left">Cumulative liquidation cost ($)</span>
        <span class="l-nt">Branching ratio n(t)</span>
        <span class="l-thr">Alarm threshold${d.threshold != null ? " (" + fmtNum(d.threshold, 4) + (d.debounce_k > 1 ? ", k=" + d.debounce_k : "") + ")" : ""}</span>
        <span class="l-fire">Alarm fires</span>
      </div>
    </div>
    <div class="panel">
      <h2>Cascade stats</h2>
      <div class="stat-row">
        <div class="stat"><div class="label">Cost (window)</div><div class="value">${fmtUsd(model.total)}</div></div>
        <div class="stat"><div class="label">Episode total</div><div class="value">${fmtUsd(d.stats.total_liquidated_usd)}</div></div>
        <div class="stat"><div class="label">Positions</div><div class="value">${d.stats.num_positions}</div></div>
        <div class="stat"><div class="label">Accounts</div><div class="value">${d.stats.num_accounts}</div></div>
        <div class="stat"><div class="label">Generations</div><div class="value">${d.stats.max_generations}</div></div>
        <div class="stat"><div class="label">Peak n(t)</div><div class="value">${fmtNum(model.peak, 4)}</div></div>
      </div>
    </div>
    <div class="panel">
      <h2>Liquidations in window (${d.liquidations.length})</h2>
      <div class="scroll-table">${liqTable(d.liquidations)}</div>
    </div>`;

  let metric = "cost", ntZoom = false;
  const canvas = root.querySelector("#chart");
  const redraw = () => drawOverlayChart(canvas, d, model, metric, ntZoom);
  redraw();
  addEventListener("resize", redraw, { passive: true });

  root.querySelectorAll("#metric-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      metric = btn.dataset.metric;
      root.querySelectorAll("#metric-toggle button").forEach((b) => b.classList.toggle("active", b === btn));
      root.querySelector("#leg-left").textContent =
        metric === "cost" ? "Cumulative liquidation cost ($)" : "Cumulative liquidation volume (# events)";
      redraw();
    });
  });
  root.querySelectorAll("#nt-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      ntZoom = btn.dataset.zoom === "zoom";
      root.querySelectorAll("#nt-toggle button").forEach((b) => b.classList.toggle("active", b === btn));
      redraw();
    });
  });
}

// Derive the cumulative-cost curve, peak n(t), threshold crossing, and lead.
function analyze(d) {
  const liq = [...d.liquidations].sort((a, b) => a.block_number - b.block_number);
  const total = liq.reduce((s, r) => s + (r.amount_usd || 0), 0);
  const totalN = liq.length;
  // cumulative cost ($) and volume (count) at each bar's end_block
  let acc = 0, accN = 0, j = 0;
  const cum = [], cumN = [];
  d.bars.forEach((b) => {
    while (j < liq.length && liq[j].block_number <= b.end_block) { acc += liq[j].amount_usd || 0; accN += 1; j++; }
    cum.push(acc); cumN.push(accN);
  });
  const peak = d.bars.reduce((m, b) => Math.max(m, b.n_t), 0);
  // Debounced alarm: fires at the k-th of a run of consecutive above-threshold
  // bars (k = d.debounce_k, default 1), matching the live monitor exactly.
  const k = d.debounce_k || 1;
  let crossIdx = -1;
  if (d.threshold != null) {
    let run = 0;
    for (let i = 0; i < d.bars.length; i++) {
      if (d.bars[i].n_t >= d.threshold) { if (++run === k) { crossIdx = i; break; } }
      else run = 0;
    }
  }
  const crossBlock = crossIdx >= 0 ? d.bars[crossIdx].end_block : null;
  // block ↔ time interpolation from liquidation timestamps
  const withT = liq.filter((r) => r.block_timestamp);
  const t0 = withT.length ? { b: withT[0].block_number, t: Date.parse(withT[0].block_timestamp) } : null;
  const t1 = withT.length ? { b: withT[withT.length - 1].block_number, t: Date.parse(withT[withT.length - 1].block_timestamp) } : null;
  const timeForBlock = (blk) => {
    if (!t0 || !t1 || t1.b === t0.b) return null;
    return t0.t + ((blk - t0.b) / (t1.b - t0.b)) * (t1.t - t0.t);
  };
  // ADR-006 metric 1: advance warning = cascade start − alarm cross (before onset).
  const advanceMin = crossBlock != null
    ? Math.round(((d.start_block - crossBlock) * 12.5) / 60)
    : null;
  // ADR-006 metric 2 (existing): alarm → 50%-of-USD cost.
  let lead = null;
  if (crossBlock != null && total > 0) {
    let a = 0, halfBlock = null;
    for (const r of liq) { a += r.amount_usd || 0; if (a >= 0.5 * total) { halfBlock = r.block_number; break; } }
    if (halfBlock != null) {
      const lb = halfBlock - crossBlock;
      lead = { lead_blocks: lb, lead_minutes: Math.round((lb * 12.5) / 60), half_block: halfBlock };
    }
  }
  return { cum, cumN, total, totalN, peak, crossIdx, crossBlock, timeForBlock, advanceMin, lead };
}

// ---- the money-shot chart ----------------------------------------------

function drawOverlayChart(canvas, d, m, metric = "cost", ntZoom = false) {
  const { ctx, w, h } = setupCanvas(canvas);
  const series = metric === "volume" ? m.cumN : m.cum;
  const fmtLeft = metric === "volume"
    ? (v) => Math.round(v).toLocaleString()
    : (v) => fmtUsd(v, 0);
  const padL = 62, padR = 46, padT = 18, padB = 30;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const lo = d.window.start_block, hi = d.window.end_block;
  const span = Math.max(hi - lo, 1);
  const maxLeft = Math.max(1, ...series);

  // n(t) right axis: full [0,1], or zoomed to the near-critical band with the
  // threshold centered (falls back to full if there's no threshold).
  const ntLo = ntZoom && d.threshold != null ? Math.max(0, 2 * d.threshold - 1) : 0;
  const ntSpan = 1 - ntLo;
  const ntDp = ntSpan < 0.05 ? 4 : 2;

  const X = (blk) => padL + ((blk - lo) / span) * plotW;
  const Ycost = (c) => padT + plotH - (c / maxLeft) * plotH;
  const Ynt = (n) => {
    const clamped = Math.min(Math.max(n, ntLo), 1);
    return padT + plotH - ((clamped - ntLo) / ntSpan) * plotH;
  };

  const dim = css("--dim"), faint = css("--faint"), border = css("--border-soft");
  const accent = css("--accent"), cost = css("--cost"), red = css("--red");
  ctx.font = "11px " + css("--mono");
  ctx.textBaseline = "middle";

  // horizontal gridlines + left ($) / right (n(t)) axis labels
  ctx.strokeStyle = border; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH * i) / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = cost; ctx.textAlign = "right";
    ctx.fillText(fmtLeft((maxLeft * (4 - i)) / 4), padL - 8, y);
    ctx.fillStyle = accent; ctx.textAlign = "left";
    ctx.fillText((ntLo + (ntSpan * (4 - i)) / 4).toFixed(ntDp), w - padR + 8, y);
  }

  // episode span shading
  ctx.fillStyle = "rgba(255,93,99,0.07)";
  ctx.fillRect(X(d.start_block), padT, X(d.end_block) - X(d.start_block), plotH);

  // x-axis time/block ticks
  ctx.fillStyle = faint; ctx.textAlign = "center";
  for (let i = 0; i <= 4; i++) {
    const blk = lo + (span * i) / 4;
    const t = m.timeForBlock(blk);
    const label = t ? new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : blk.toLocaleString();
    ctx.fillText(label, X(blk), h - padB + 14);
  }

  // cumulative cost area (filled gradient)
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  grad.addColorStop(0, "rgba(255,180,84,0.42)");
  grad.addColorStop(1, "rgba(255,180,84,0.03)");
  ctx.beginPath();
  ctx.moveTo(X(d.bars[0].end_block), Ycost(0));
  d.bars.forEach((b, i) => ctx.lineTo(X(b.end_block), Ycost(series[i])));
  ctx.lineTo(X(d.bars[d.bars.length - 1].end_block), Ycost(0));
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  // cost outline
  ctx.beginPath();
  d.bars.forEach((b, i) => (i ? ctx.lineTo(X(b.end_block), Ycost(series[i])) : ctx.moveTo(X(b.end_block), Ycost(series[i]))));
  ctx.strokeStyle = cost; ctx.lineWidth = 1.5; ctx.stroke();

  // threshold line
  if (d.threshold != null) {
    ctx.strokeStyle = red; ctx.lineWidth = 1.25; ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(padL, Ynt(d.threshold)); ctx.lineTo(w - padR, Ynt(d.threshold)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // n(t) line (glow + core)
  const drawNt = (lw, style) => {
    ctx.beginPath();
    d.bars.forEach((b, i) => (i ? ctx.lineTo(X(b.end_block), Ynt(b.n_t)) : ctx.moveTo(X(b.end_block), Ynt(b.n_t))));
    ctx.strokeStyle = style; ctx.lineWidth = lw; ctx.stroke();
  };
  drawNt(5, "rgba(110,168,255,0.18)");
  drawNt(2, accent);

  // "alarm fires" marker
  if (m.crossIdx >= 0) {
    const bx = X(d.bars[m.crossIdx].end_block);
    ctx.strokeStyle = red; ctx.lineWidth = 1.5; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(bx, padT); ctx.lineTo(bx, padT + plotH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = red; ctx.beginPath(); ctx.arc(bx, Ynt(d.bars[m.crossIdx].n_t), 3.5, 0, 7); ctx.fill();
    ctx.textAlign = bx > w * 0.6 ? "right" : "left";
    ctx.fillStyle = red; ctx.font = "700 11px " + css("--mono");
    ctx.fillText("ALARM FIRES", bx + (bx > w * 0.6 ? -8 : 8), padT + 10);
  }
}

// small n(t)+cost overlay for cards
function drawMiniOverlay(canvas, preview, threshold) {
  if (!preview || !preview.nt.length) return;
  const { ctx, w, h } = setupCanvas(canvas);
  const n = preview.nt.length;
  const X = (i) => (i / Math.max(n - 1, 1)) * w;
  const Y = (v) => h - Math.min(v, 1) * (h - 2) - 1;
  // cost area
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(255,180,84,0.35)"); grad.addColorStop(1, "rgba(255,180,84,0.02)");
  ctx.beginPath(); ctx.moveTo(0, h);
  preview.cost.forEach((c, i) => ctx.lineTo(X(i), Y(c)));
  ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  // threshold
  if (threshold != null) {
    ctx.strokeStyle = css("--red"); ctx.globalAlpha = 0.6; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(0, Y(threshold)); ctx.lineTo(w, Y(threshold)); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  // n(t)
  ctx.beginPath();
  preview.nt.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
  ctx.strokeStyle = css("--accent"); ctx.lineWidth = 1.75; ctx.stroke();
}

function liqTable(rows) {
  if (!rows.length) return `<div class="empty-note">No liquidations in window.</div>`;
  const body = rows
    .map((r) => `<tr>
      <td>${fmtBlock(r.block_number)}</td>
      <td>${fmtTime(r.block_timestamp)}</td>
      <td class="cost">${fmtUsd(r.amount_usd)}</td>
      <td>${shortHex(r.user)}</td>
      <td>${shortHex(r.collateral_asset)}</td>
      <td>${shortHex(r.debt_asset)}</td>
    </tr>`).join("");
  return `<table><tr><th>block</th><th>time</th><th>debt repaid</th><th>user</th><th>collateral</th><th>debt</th></tr>${body}</table>`;
}
