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

// ── HTML generators (shared by live API + static snapshot mode) ──
function bodyHTML(b) {
  if (!b || !b.body) return `<div class="note">no on-disk body</div>`;
  return `<div class="tag">${fileLine(b.file, b.start_line)}</div><pre>${esc(b.body)}</pre>`;
}
function opsRowsHTML(rows) {
  if (!rows || !rows.length) return `<tr><td colspan="5" class="note">none</td></tr>`;
  return rows.map((r) =>
    `<tr class="row" onclick="center('${esc(r.impl_symbol)}','${esc(r.impl_name)}','function')">
       <td>${esc(r.ops_name)}<br><span class="tag">${fileLine(r.file_path, r.line)}</span></td>
       <td><b>${esc(r.impl_name)}</b></td>
       <td>${esc(r.field_name || "")}</td>
       <td class="r">${fileLine(r.file_path, r.line)}</td>
       <td class="r">${r.confidence ?? ""}</td>
     </tr>`).join("");
}
function chainHTML(chain) {
  if (!chain || !chain.length) return `<span class="err">not found</span>`;
  return `<div style="font-family:ui-monospace,monospace;font-size:13px;line-height:1.9">` +
    chain.map((n) => {
      const loc = n.line != null ? fileLine(n.file_path, n.line) : "(target)";
      const cls = n.depth === 0 ? `style="color:#cf222e;font-weight:600"` : "";
      const arr = n.depth > 0 ? `<span class="tag">  &uarr; called by</span><br>` : "";
      return `${arr}<span ${cls} style="cursor:pointer" onclick="center('${esc(n.scip_symbol)}','${esc(n.name)}','${esc(n.kind)}')">${esc(n.name)}</span> <span class="tag">(${esc(n.kind)})</span> <span class="tag">${loc}</span>`;
    }).join("") + `</div>`;
}
function structHTML(layout) {
  const fields = (layout && layout.fields) || [];
  if (!fields.length) return `<div class="note">no fields recorded (struct may be external / opaque, or the index predates contains-edge derivation).</div>`;
  return `<table><thead><tr><th>#</th><th>field</th><th>kind</th><th>signature</th></tr></thead><tbody>` +
    fields.map((f, i) => `<tr class="row" onclick="center('${esc(f.scip_symbol)}','${esc(f.name)}','${esc(f.kind)}')">
      <td class="r">${i + 1}</td><td><b>.${esc(f.name)}</b></td><td>${esc(f.kind)}</td>
      <td><span class="tag">${esc(f.signature || "")}</span></td></tr>`).join("") + `</tbody></table>`;
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
  if (SNAPSHOT_MODE) {
    // No live API on the static Pages demo — hint to run locally.
    $("demoOut").insertAdjacentHTML("afterbegin",
      `<div class="note">Run <span class="tag">kgraph view</span> locally to explore <b>${esc(name)}</b> interactively.</div>`);
    return;
  }
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
    $("body").innerHTML = bodyHTML(body);
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
    $("opsTable").querySelector("tbody").innerHTML = opsRowsHTML(rows);
  } catch (e) { $("opsCount").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

// ── call chain ──
async function doChain() {
  const name = $("chainName").value.trim();
  if (!name) return;
  $("chainCount").textContent = "tracing…";
  try {
    const chain = await api(`/api/callchain?name=${encodeURIComponent(name)}&max_depth=20`);
    $("chainCount").textContent = chain.length ? `${chain.length} levels` : "";
    $("chainOut").innerHTML = chainHTML(chain);
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

// ── snapshot mode (static Pages: no live API) ──
let SNAPSHOT_MODE = false;
const SNAPSHOTS = {};
async function detectMode() {
  try {
    const s = await api("/api/status");
    $("dbtag").textContent = `kgraph.db · ${(s.metadata || {}).total_symbols || "?"} symbols`;
  } catch {
    SNAPSHOT_MODE = true;
    enterSnapshotMode();   // no API → static demo
  }
}
async function enterSnapshotMode() {
  document.querySelectorAll(".bar,.tabs,#paneGraph,#paneOps,#paneChain")
    .forEach((e) => { e.style.display = "none"; });
  $("demoPanel").style.display = "block";
  $("dbtag").textContent = "static demo (Pages)";
  try {
    const man = await api("data/manifest.json");
    $("demoSelect").innerHTML = man.map((m) =>
      `<option value="${esc(m.key)}">${esc(m.title)}</option>`).join("");
    man.forEach((m) => { SNAPSHOTS[m.key] = m; });
    if (man[0]) loadSnapshot(man[0].key);
  } catch (e) {
    $("demoOut").innerHTML = `<span class="err">no snapshots available: ${esc(e.message)}</span>`;
  }
}
async function loadSnapshot(key) {
  const m = SNAPSHOTS[key];
  if (!m) return;
  try {
    const data = await api(m.file);
    $("demoTitle").textContent = `(${m.count} items)`;
    let html = "";
    if (m.view === "ops") html = `<table><thead><tr><th>ops table</th><th>→ impl</th><th>field</th><th>file:line</th><th>conf</th></tr></thead><tbody>${opsRowsHTML(data)}</tbody></table>`;
    else if (m.view === "callchain") html = chainHTML(data);
    else if (m.view === "struct") html = structHTML(data);
    else if (m.view === "body") html = bodyHTML(data);
    $("demoOut").innerHTML = html || `<span class="err">unknown view: ${esc(m.view)}</span>`;
  } catch (e) { $("demoOut").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
window.loadSnapshot = loadSnapshot;

// ── wire ──
$("searchBtn").addEventListener("click", doSearch);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
$("opsBtn").addEventListener("click", doOps);
$("field").addEventListener("keydown", (e) => { if (e.key === "Enter") doOps(); });
$("chainBtn").addEventListener("click", doChain);
$("chainName").addEventListener("keydown", (e) => { if (e.key === "Enter") doChain(); });

detectMode();   // interactive (local kgraph view) or snapshot demo (Pages)
