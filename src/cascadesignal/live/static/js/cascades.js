// Cascades panel: liquidation-clustering heuristic (live/cascades.py), NOT
// the ADR-001 D-A cascade definition. See /api/cascades/config's "note".
// Renders a table of this session's clusters into `container`, most recent
// first; each row links to cascade.html for the detail/replay view.

function durationSeconds(cascade) {
  if (!cascade.start_time || !cascade.end_time) return null;
  return (new Date(cascade.end_time) - new Date(cascade.start_time)) / 1000;
}

function cascadeRow(protocol, c) {
  const href = `cascade.html?protocol=${encodeURIComponent(protocol)}&id=${encodeURIComponent(c.cascade_id)}`;
  const status = c.status === "ongoing"
    ? '<span class="badge pending">ongoing</span>'
    : "";
  // Three distinct outcomes, not two: warned BEFORE onset (the thing we want),
  // fired only once already underway (too late to be a warning), or silent.
  let outcome, lead;
  if (c.early_warned) {
    outcome = '<span class="badge" style="background:rgba(61,220,132,0.2);color:var(--green)">early warning</span>';
    lead = `${c.lead_blocks}b · ${fmtDuration(c.lead_seconds)}`;
  } else if (c.alert_fired) {
    outcome = '<span class="badge" style="background:rgba(245,185,66,0.2);color:var(--amber)">fired late</span>';
    lead = "—";
  } else {
    outcome = '<span class="badge" style="background:rgba(128,128,128,0.15);color:var(--text-dim)">no alert</span>';
    lead = "—";
  }
  return `
    <tr>
      <td><a href="${href}">${c.cascade_id}</a> ${status}</td>
      <td>${fmtTime(c.start_time)}</td>
      <td>${fmtDuration(durationSeconds(c))}</td>
      <td>${fmtBlock(c.start_block)}${c.end_block ? ` +${c.end_block - c.start_block}` : ""}</td>
      <td>${c.n_liquidations}</td>
      <td>${outcome}</td>
      <td>${lead}</td>
    </tr>`;
}

async function renderCascadesPanel(container, protocol) {
  container.innerHTML = `<div class="empty-note">Loading cascades…</div>`;
  try {
    const res = await fetch(`/api/cascades/${encodeURIComponent(protocol)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cascades = await res.json();
    if (!cascades.length) {
      container.innerHTML = `<div class="empty-note">No cascades clustered yet this session.</div>`;
      return;
    }
    // Horizontal scroll rather than silent truncation: with the lead column
    // this is 7 monospace columns inside a ~700px card.
    container.innerHTML = `
      <div style="overflow-x:auto">
      <table>
        <tr><th>id</th><th>start</th><th>dur</th><th>block +span</th><th>liq</th><th>algorithm</th><th>lead</th></tr>
        ${cascades.map(c => cascadeRow(protocol, c)).join("")}
      </table>
      </div>`;
  } catch (e) {
    container.innerHTML = `<div class="empty-note">Failed to load cascades: ${e}</div>`;
  }
}
