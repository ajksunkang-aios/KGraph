"use strict";
// KGraph Explorer — vanilla JS + Cytoscape.js. Talks same-origin to view/server.py.
const CAP = 200;                  // max nodes rendered in the graph (perf/token bound)
const $ = (id) => document.getElementById(id);

let cy = null;
let centeredScip = null;

// ── helpers ──
async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(e.error || `HTTP ${r.status}`);
  }
  return r.json();
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
const KIND_COLOR = {
  function: "#0969da", struct: "#1a7f37", field: "#6f42c1",
  macro: "#d1242f", typedef: "#8250df", global_var: "#57606a",
};
function nodeStyle(kind) {
  return { "background-color": KIND_COLOR[kind] || "#57606a", label: "name",
           color: "#1f2328", "font-size": 11, width: 34, height: 34,
           "text-valign": "bottom", "text-margin-y": 4 };
}
function fileLine(file, line) {
  // line is 0-based from API; +1 for display
  return `${file ? esc(file) : "?"}:${line != null ? line + 1 : "?"}`;
}

// ── search → center ──
async function doSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  const kind = $("kind").value;
  $("centerTag").textContent = "searching…";
  try {
    const res = await api(`/api/search?q=${encodeURIComponent(q)}${kind ? "&kind=" + kind : ""}&limit=15`);
    if (!res.length) { $("centerTag").innerHTML = `<span class="err">no match</span>`; return; }
    if (res.length === 1) { center(res[0].scip_symbol, res[0].name, res[0].kind); return; }
    // multiple: pick the first as center, list alternatives in body pane
    center(res[0].scip_symbol, res[0].name, res[0].kind);
    $("body").innerHTML = `<div class="note">Multiple matches, centered on <b>${esc(res[0].name)}</b>. Others:<br>` +
      res.slice(1).map((r) => `<a href="#" onclick="center('${esc(r.scip_symbol)}','${esc(r.name)}','${esc(r.kind)}');return false">${esc(r.name)} <span class="tag">(${esc(r.kind)})</span></a>`).join("<br>") + `</div>`;
  } catch (e) { $("centerTag").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

// ── center a symbol: neighborhood + callers + callees + body ──
async function center(scip, name, kind) {
  centeredScip = scip;
  $("centerTag").innerHTML = `centered: <b>${esc(name)}</b> <span class="pill">${esc(kind || "")}</span>`;
  $("q").value = name;
  try {
    const [nb, callers, callees, body] = await Promise.all([
      api(`/api/neighborhood?name=${encodeURIComponent(name)}&depth=1`),
      api(`/api/callers?name=${encodeURIComponent(name)}&depth=1&limit=50`),
      api(`/api/callees?name=${encodeURIComponent(name)}&depth=1&limit=50`),
      api(`/api/body?name=${encodeURIComponent(name)}`),
    ]);
    drawGraph(nb, scip, name);
    renderList("callers", callers, "← calls");
    renderList("callees", callees, "calls →");
    $("body").innerHTML = body.body
      ? `<div class="tag">${fileLine(body.file, body.start_line)}</div><pre>${esc(body.body)}</pre>`
      : `<div class="note">no on-disk body</div>`;
  } catch (e) {
    $("centerTag").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

function renderList(el, rows, label) {
  if (!rows || !rows.length) { $(el).innerHTML = `<li class="note">(none)</li>`; return; }
  $(el).innerHTML = rows.slice(0, 50).map((r) =>
    `<li title="${fileLine(r.file_path, r.line)}" onclick="center('${esc(r.scip_symbol)}','${esc(r.name)}','${esc(r.kind)}')">
       ${esc(r.name)} <span class="tag">${esc(r.kind)}</span>${r.edge_type === "ops_bind" ? ' <span class="pill ops">ops</span>' : ""}
     </li>`).join("");
}

// ── Cytoscape graph ──
function drawGraph(nb, centerScip, centerName) {
  const nodes = (nb.nodes || []).slice();
  const center = { data: { id: centerScip, name: centerName, kind: (nb.center && nb.center.kind) || "function", center: true } };
  const elements = [center];
  const ids = new Set([centerScip]);
  let shown = 0, truncated = 0;
  for (const n of nodes) {
    const id = n.scip_symbol;
    if (!id || ids.has(id)) continue;
    if (shown >= CAP) { truncated++; continue; }
    ids.add(id); shown++;
    elements.push({ data: { id, name: n.name, kind: n.kind } });
    // edge: center <-> neighbor (undirected for neighborhood)
    elements.push({ data: { id: `e${shown}`, source: centerScip, target: id } });
  }
  $("cap").textContent = truncated ? `showing ${shown} of ${shown + truncated} neighbors (capped at ${CAP})` : `${shown} neighbors`;

  if (cy) { cy.destroy(); }
  cy = cytoscape({
    container: $("cy"),
    elements,
    style: [
      { selector: "node", style: { ...nodeStyle("x"), label: "data(name)" } },
      { selector: "node[center]", style: { "background-color": "#cf222e", width: 44, height: 44,
          "border-width": 2, "border-color": "#1f2328" } },
      { selector: "node[kind='function']", style: { "background-color": KIND_COLOR.function } },
      { selector: "node[kind='struct']", style: { "background-color": KIND_COLOR.struct } },
      { selector: "node[kind='field']", style: { "background-color": KIND_COLOR.field } },
      { selector: "edge", style: { "line-color": "#d0d7de", width: 1, "target-arrow-color": "#d0d7de",
          "target-arrow-shape": "none", "curve-style": "bezier" } },
    ],
    layout: { name: "concentric", concentric: (n) => n.data("center") ? 10 : 1,
              levelWidth: () => 1, animate: false, minNodeSpacing: 24 },
  });
  cy.on("tap", "node", (evt) => {
    const n = evt.target;
    center(n.id(), n.data("name"), n.data("kind"));
  });
}

// ── ops table ──
async function doOps() {
  const field = $("field").value.trim();
  if (!field) return;
  const st = $("st").value.trim();
  try {
    const rows = await api(`/api/ops?field=${encodeURIComponent(field)}${st ? "&struct_type=" + encodeURIComponent(st) : ""}`);
    $("opsCount").textContent = `${rows.length} implementation(s)`;
    $("opsTable").querySelector("tbody").innerHTML = rows.length ? rows.map((r) =>
      `<tr class="row" onclick="center('${esc(r.impl_symbol)}','${esc(r.impl_name)}','function')">
         <td>${esc(r.ops_name)}<br><span class="tag">${fileLine(r.file_path, r.line)}</span></td>
         <td><b>${esc(r.impl_name)}</b></td>
         <td>${esc(r.field_name || field)}</td>
         <td class="r">${fileLine(r.file_path, r.line)}</td>
         <td class="r">${r.confidence ?? ""}</td>
       </tr>`).join("") : `<tr><td colspan="5" class="note">none</td></tr>`;
  } catch (e) { $("opsCount").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

// ── call chain ──
async function doChain() {
  const name = $("chainName").value.trim();
  if (!name) return;
  $("chainCount").textContent = "tracing…";
  try {
    const chain = await api(`/api/callchain?name=${encodeURIComponent(name)}&max_depth=20`);
    if (!chain.length) { $("chainOut").innerHTML = `<span class="err">not found</span>`; $("chainCount").textContent = ""; return; }
    $("chainCount").textContent = `${chain.length} levels`;
    $("chainOut").innerHTML = `<div style="font-family:ui-monospace,monospace;font-size:13px;line-height:1.9">` +
      chain.map((n, i) => {
        const loc = n.line != null ? fileLine(n.file_path, n.line) : "(target)";
        const cls = n.depth === 0 ? `style="color:#cf222e;font-weight:600"` : "";
        const arr = n.depth > 0 ? `<span class="tag">  &uarr; called by</span><br>` : "";
        return `${arr}<span ${cls} style="cursor:pointer" onclick="center('${esc(n.scip_symbol)}','${esc(n.name)}','${esc(n.kind)}')">${esc(n.name)}</span> <span class="tag">(${esc(n.kind)})</span> <span class="tag">${loc}</span>`;
      }).join("") + `</div>`;
  } catch (e) { $("chainCount").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

// ── tabs ──
function showTab(t) {
  const map = { graph: "paneGraph", ops: "paneOps", chain: "paneChain" };
  for (const k of Object.keys(map)) {
    $(map[k]).style.display = (t === k) ? (k === "graph" ? "grid" : "block") : "none";
  }
  $("tabGraph").classList.toggle("active", t === "graph");
  $("tabOps").classList.toggle("active", t === "ops");
  $("tabChain").classList.toggle("active", t === "chain");
  if (t === "graph" && cy) { setTimeout(() => cy.resize(), 50); }
}
window.showTab = showTab;
window.center = center;

// ── wire ──
$("searchBtn").addEventListener("click", doSearch);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
$("opsBtn").addEventListener("click", doOps);
$("field").addEventListener("keydown", (e) => { if (e.key === "Enter") doOps(); });
$("chainBtn").addEventListener("click", doChain);
$("chainName").addEventListener("keydown", (e) => { if (e.key === "Enter") doChain(); });

// status badge (db / counts)
api("/api/status").then((s) => {
  const m = s.metadata || {};
  $("dbtag").textContent = `kgraph.db · ${m.total_symbols || "?"} symbols`;
}).catch(() => {});
