// Shared formatting helpers -- used by index.html and cascade.html.

function fmtNum(x, d = 4) {
  return (x == null) ? "—" : Number(x).toFixed(d);
}

function fmtBlock(x) {
  return (x == null) ? "—" : x.toLocaleString();
}

function shortHex(h, n = 6) {
  if (!h) return "—";
  return h.slice(0, 2 + n) + "…" + h.slice(-4);
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
