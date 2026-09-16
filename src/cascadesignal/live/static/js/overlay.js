// Reusable n(t)-vs-liquidations overlay, used by the live protocol cards
// (index.html). The historical results page has its own richer variant
// (episode shading, fire marker, dual metric) in historical.js — this is the
// compact live version: cumulative liquidation volume area + n(t) line +
// threshold, with an optional near-critical zoom on the n(t) axis.

function _cssVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}

// cfg: { xs:[block], nts:[n_t], left:[cumulative volume], threshold, ntZoom,
//        leftFmt? }
function drawNtOverlay(canvas, cfg) {
  const dpr = devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const { xs, nts, left, threshold, ntZoom } = cfg;
  if (!xs || xs.length < 2) {
    ctx.fillStyle = _cssVar("--faint");
    ctx.font = "12px " + _cssVar("--sans");
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("waiting for live bars…", w / 2, h / 2);
    return;
  }
  const leftFmt = cfg.leftFmt || ((v) => Math.round(v).toLocaleString());
  const padL = 50, padR = 38, padT = 10, padB = 16;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const lo = xs[0], hi = xs[xs.length - 1], span = Math.max(hi - lo, 1);
  const maxLeft = Math.max(1, ...left);

  const ntLo = ntZoom && threshold != null ? Math.max(0, 2 * threshold - 1) : 0;
  const ntSpan = 1 - ntLo;
  const ntDp = ntSpan < 0.05 ? 4 : 2;

  const X = (x) => padL + ((x - lo) / span) * plotW;
  const YL = (v) => padT + plotH - (v / maxLeft) * plotH;
  const YN = (n) => {
    const c = Math.min(Math.max(n, ntLo), 1);
    return padT + plotH - ((c - ntLo) / ntSpan) * plotH;
  };

  const border = _cssVar("--border-soft"), accent = _cssVar("--accent");
  const cost = _cssVar("--cost"), red = _cssVar("--red");
  ctx.font = "10px " + _cssVar("--mono"); ctx.textBaseline = "middle";

  for (let i = 0; i <= 2; i++) {
    const y = padT + (plotH * i) / 2;
    ctx.strokeStyle = border; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = cost; ctx.textAlign = "right";
    ctx.fillText(leftFmt((maxLeft * (2 - i)) / 2), padL - 6, y);
    ctx.fillStyle = accent; ctx.textAlign = "left";
    ctx.fillText((ntLo + (ntSpan * (2 - i)) / 2).toFixed(ntDp), w - padR + 6, y);
  }

  // cumulative volume area
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  grad.addColorStop(0, "rgba(255,180,84,0.32)");
  grad.addColorStop(1, "rgba(255,180,84,0.02)");
  ctx.beginPath(); ctx.moveTo(X(xs[0]), YL(0));
  xs.forEach((x, i) => ctx.lineTo(X(x), YL(left[i])));
  ctx.lineTo(X(xs[xs.length - 1]), YL(0));
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  xs.forEach((x, i) => (i ? ctx.lineTo(X(x), YL(left[i])) : ctx.moveTo(X(x), YL(left[i]))));
  ctx.strokeStyle = cost; ctx.lineWidth = 1.25; ctx.stroke();

  // threshold
  if (threshold != null) {
    ctx.strokeStyle = red; ctx.lineWidth = 1; ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(padL, YN(threshold)); ctx.lineTo(w - padR, YN(threshold)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // n(t) line (glow + core)
  const line = (lw, style) => {
    ctx.beginPath();
    xs.forEach((x, i) => (i ? ctx.lineTo(X(x), YN(nts[i])) : ctx.moveTo(X(x), YN(nts[i]))));
    ctx.strokeStyle = style; ctx.lineWidth = lw; ctx.stroke();
  };
  line(4, "rgba(110,168,255,0.18)");
  line(1.75, accent);
}
