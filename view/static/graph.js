"use strict";

// KGraph Explorer — a bounded, evidence-backed local graph viewer.  The API
// always exchanges SCIP ids after search so duplicate short names never change
// the definition the user selected.
const $ = (id) => document.getElementById(id);
const QUERY = new URLSearchParams(window.location.search);
const VIDEO_MODE = QUERY.get("demo") === "video";
const NODE_COLORS = {
  function: "#38bdf8", struct: "#34d399", field: "#c084fc",
  macro: "#fb7185", typedef: "#fbbf24", global_var: "#94a3b8",
  enum: "#fb923c", union: "#fb923c", variable: "#94a3b8",
};
const EDGE_STYLES = {
  calls: { color: "#60a5fa", lineStyle: "solid" },
  ops_bind: { color: "#fbbf24", lineStyle: "dashed" },
  references: { color: "#c084fc", lineStyle: "dotted" },
  contains: { color: "#34d399", lineStyle: "dashed" },
  implements: { color: "#2dd4bf", lineStyle: "solid" },
  type_of: { color: "#a78bfa", lineStyle: "dotted" },
};
const GLOBAL_NODE_COLORS = ["#38bdf8", "#34d399", "#c084fc", "#fbbf24", "#fb7185", "#2dd4bf", "#fb923c"];

const state = {
  cy: null,
  globalCy: null,
  fragment: null,
  globalNetwork: null,
  centeredScip: null,
  globalPrefix: "",
  selectedScip: null,
  globalSelectedGroup: null,
  selectedEdgeId: null,
  globalSelectedEdgeId: null,
  fileSymbolsPath: null,
  fileSymbolsRequest: 0,
  fileSymbolsNextOffset: null,
  fileSymbolsLoaded: 0,
  fileSymbolsLoadingMore: false,
  snapshotMode: false,
  demoTimers: [],
  demoSymbol: null,
};

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || `HTTP ${response.status}`);
  }
  return response.json();
}

function fileLine(file, line) {
  if (!file) return "no on-disk location";
  return `${file}:${line != null && line >= 0 ? line + 1 : "?"}`;
}

function displayName(node) {
  return node && node.name ? node.name : "unknown symbol";
}

function fileSymbolKindLabel(kind) {
  return kind === "global_var" ? "Global variable" : (kind || "symbol");
}

function selectedEdgeTypes() {
  return [...document.querySelectorAll("#edgeTypes input:checked")].map((input) => input.value);
}

function selectedGlobalEdgeTypes() {
  return [...document.querySelectorAll("#globalEdgeTypes input:checked")].map((input) => input.value);
}

function globalNodeColor(id) {
  let hash = 0;
  for (const char of String(id)) hash = ((hash << 5) - hash) + char.charCodeAt(0);
  return GLOBAL_NODE_COLORS[Math.abs(hash) % GLOBAL_NODE_COLORS.length];
}

function nodeForScip(scip) {
  if (!state.fragment) return null;
  if (state.fragment.center && state.fragment.center.scip_symbol === scip) {
    return state.fragment.center;
  }
  return (state.fragment.nodes || []).find((node) => node.scip_symbol === scip) || null;
}

function setText(id, text, className = "") {
  const element = $(id);
  element.textContent = text;
  element.className = className;
  return element;
}

function clearSearchResults() {
  const results = $("searchResults");
  results.replaceChildren();
  results.classList.remove("open");
}

function rankCandidates(results, query) {
  const normalized = query.toLowerCase();
  return [...results].sort((left, right) => {
    const leftExact = left.name && left.name.toLowerCase() === normalized ? 0 : 1;
    const rightExact = right.name && right.name.toLowerCase() === normalized ? 0 : 1;
    if (leftExact !== rightExact) return leftExact - rightExact;
    const leftDef = left.def_start_line != null && left.def_start_line >= 0 ? 0 : 1;
    const rightDef = right.def_start_line != null && right.def_start_line >= 0 ? 0 : 1;
    if (leftDef !== rightDef) return leftDef - rightDef;
    return String(left.def_file_path || "").localeCompare(String(right.def_file_path || ""));
  });
}

function renderSearchResults(results) {
  const container = $("searchResults");
  container.replaceChildren();
  for (const result of results.slice(0, 12)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-result";
    button.setAttribute("role", "option");
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = displayName(result);
    const location = document.createElement("span");
    location.textContent = fileLine(result.def_file_path, result.def_start_line);
    copy.append(name, location);
    const kind = document.createElement("span");
    kind.className = "kind-pill";
    kind.textContent = result.kind || "symbol";
    button.append(copy, kind);
    button.addEventListener("click", () => loadSymbol(result.scip_symbol));
    container.append(button);
  }
  container.classList.toggle("open", Boolean(container.childElementCount));
}

async function doSearch() {
  const query = $("q").value.trim();
  if (!query || state.snapshotMode) return;
  setText("centerTag", "Searching indexed symbols…");
  try {
    const kind = $("kind").value;
    const results = await api(
      `/api/search?q=${encodeURIComponent(query)}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}&limit=20`,
    );
    const candidates = rankCandidates(results, query);
    if (!candidates.length) {
      clearSearchResults();
      setText("centerTag", "No indexed symbol matched that search", "error");
      return;
    }
    const exact = candidates.filter((candidate) => candidate.name === query);
    if (exact.length === 1) {
      clearSearchResults();
      await loadSymbol(exact[0].scip_symbol);
      return;
    }
    renderSearchResults(candidates);
    setText("centerTag", `Choose one of ${candidates.length} matching symbols`);
  } catch (error) {
    clearSearchResults();
    setText("centerTag", error.message, "error");
  }
}

function setFragmentStatus(fragment) {
  const limits = fragment.limits || {};
  const count = `${(fragment.nodes || []).length + 1} nodes · ${(fragment.edges || []).length} relationships`;
  const clipped = fragment.truncated
    ? ` · limited to ${limits.max_nodes || "?"} nodes / ${limits.max_edges || "?"} edges`
    : "";
  setText("fragmentStatus", `${count}${clipped}`);
  const details = fragment.truncation || {};
  const note = fragment.truncated
    ? `This fragment was safely truncated (${details.nodes ? "node" : ""}${details.nodes && details.edges ? " + " : ""}${details.edges ? "edge" : ""} limit). Narrow edge types or return to 1 hop for a clearer view.`
    : "Directed multi-edges preserve relationship type, confidence, and source evidence.";
  setText("cap", note);
}

function cytoscapeElements(fragment) {
  const nodes = [fragment.center, ...(fragment.nodes || [])]
    .filter(Boolean)
    .map((node) => ({
      data: {
        id: node.scip_symbol,
        name: displayName(node),
        kind: node.kind || "symbol",
        color: NODE_COLORS[node.kind] || "#94a3b8",
        center: node.scip_symbol === fragment.center_symbol,
      },
      classes: node.scip_symbol === fragment.center_symbol ? "center" : "",
    }));
  const knownNodes = new Set(nodes.map((node) => node.data.id));
  const edges = (fragment.edges || [])
    .filter((edge) => knownNodes.has(edge.source) && knownNodes.has(edge.target))
    .map((edge) => {
      const style = EDGE_STYLES[edge.type] || { color: "#9fb1ca", lineStyle: "dotted" };
      return {
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          type: edge.type,
          confidence: edge.confidence,
          color: style.color,
          lineStyle: style.lineStyle,
        },
      };
    });
  return [...nodes, ...edges];
}

function layoutOptions() {
  const layout = $("layout").value;
  if (layout === "flow") {
    return {
      name: "breadthfirst", directed: true, roots: state.centeredScip ? [state.centeredScip] : undefined,
      padding: 46, spacingFactor: 1.35, animate: false,
    };
  }
  if (layout === "rings") {
    return {
      name: "concentric", padding: 42, minNodeSpacing: 32, animate: false,
      concentric: (node) => (node.data("center") ? 10 : 1), levelWidth: () => 1,
    };
  }
  return {
    name: "cose", padding: 42, animate: false, randomize: false,
    nodeRepulsion: 9000, idealEdgeLength: 110, nodeOverlap: 12, gravity: .36,
    numIter: 450,
  };
}

function runLayout() {
  if (state.cy) state.cy.layout(layoutOptions()).run();
}

function globalNodeForId(id) {
  return state.globalNetwork && (state.globalNetwork.nodes || []).find((node) => node.id === id);
}

function isFileNetworkNode(node) {
  return Boolean(node && node.is_file);
}

function setGlobalNetworkStatus(network) {
  const scope = network.scope || {};
  const totals = network.totals || {};
  const nodes = network.nodes || [];
  const edges = network.edges || [];
  const limits = network.limits || {};
  const clipped = network.truncated
    ? ` · limited to ${limits.max_nodes || "?"} modules / ${limits.max_edges || "?"} links`
    : "";
  setText("networkTitle", `${scope.label || "Linux"} code network`);
  const itemLabel = nodes.some((node) => isFileNetworkNode(node)) ? "items" : "modules";
  setText("networkStatus", `${nodes.length} ${itemLabel} · ${edges.length} dependency links${clipped}`);
  setText("networkScope", scope.label || "Linux");
  $("networkBackBtn").disabled = !scope.prefix;
  const scopeFiles = Number(totals.files || 0).toLocaleString();
  const scopeSymbols = Number(totals.symbols || 0).toLocaleString();
  const scopeRelationships = Number(totals.relationships || 0).toLocaleString();
  const context = scope.prefix
    ? `This scope covers ${scopeFiles} source files and ${scopeSymbols} locally-defined symbols. “outside scope” collapses cross-module dependencies.`
    : `This source map covers ${scopeFiles} indexed source files and ${scopeSymbols} locally-defined symbols.`;
  setText(
    "networkCap",
    `${context} ${scopeRelationships} real indexed relationships were aggregated by directory or source file; no raw kernel-wide symbol graph is sent to the browser.`,
  );
}

function globalCytoscapeElements(network) {
  const nodes = network.nodes || [];
  const maxRelationships = Math.max(1, ...nodes.map((node) => Number(node.relationships || 0)));
  const elements = nodes.map((node) => {
    const isFile = isFileNetworkNode(node);
    const classes = [];
    if (node.id.startsWith("@")) classes.push("outside");
    if (isFile) classes.push("file");
    return {
      data: {
        id: node.id,
        label: node.label || node.id,
        path: node.path || "",
        files: Number(node.files || 0),
        symbols: Number(node.symbols || 0),
        incoming: Number(node.incoming || 0),
        outgoing: Number(node.outgoing || 0),
        internal: Number(node.internal || 0),
        relationships: Number(node.relationships || 0),
        canDrill: Boolean(node.can_drill),
        isFile,
        color: node.id.startsWith("@") ? "#64748b" : globalNodeColor(node.id),
        nodeSize: node.id.startsWith("@")
          ? 40
          : (isFile ? 28 : 30) + Math.min(isFile ? 28 : 40, Math.round(36 * Math.sqrt(Number(node.relationships || 0) / maxRelationships))),
      },
      classes: classes.join(" "),
    };
  });
  for (const edge of network.edges || []) {
    const style = EDGE_STYLES[edge.type] || { color: "#9fb1ca", lineStyle: "dotted" };
    elements.push({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: edge.type,
        relationships: Number(edge.relationships || 0),
        weight: Number(edge.weight || 0),
        color: style.color,
        lineStyle: style.lineStyle,
        lineWidth: 1 + Math.min(5.5, Math.log2(Number(edge.relationships || 0) + 1) / 2),
      },
    });
  }
  return elements;
}

function globalLayoutOptions() {
  if ($("networkLayout").value === "rings") {
    return {
      name: "concentric", padding: 54, minNodeSpacing: 28, animate: false,
      concentric: (node) => node.data("relationships") || 0,
      levelWidth: () => 1,
    };
  }
  return {
    name: "cose", padding: 56, animate: false, randomize: false,
    nodeRepulsion: 10000, idealEdgeLength: 135, nodeOverlap: 18, gravity: .3,
    numIter: 620,
  };
}

function runGlobalLayout() {
  if (state.globalCy) state.globalCy.layout(globalLayoutOptions()).run();
}

function clearGlobalSelection() {
  if (state.globalCy) state.globalCy.elements().removeClass("selected related faded selected-edge");
  state.globalSelectedGroup = null;
  state.globalSelectedEdgeId = null;
  setText("networkSelectionName", "No selection");
  $("networkSelectionKind").style.display = "none";
  setText("networkSelectionPath", "Select a module to inspect its indexed code volume and dependency flow.");
  setText("networkMetrics", "The overview is grouped by directories, not sampled symbols.");
  $("networkEnterBtn").disabled = true;
  $("networkEnterBtn").textContent = "Explore module";
  setText("networkEvidence", "Select an edge to see the aggregated relationship count and direction.");
}

function selectGlobalNode(id) {
  const node = globalNodeForId(id);
  if (!node) return;
  const isFile = isFileNetworkNode(node);
  state.globalSelectedGroup = id;
  state.globalSelectedEdgeId = null;
  if (state.globalCy) {
    state.globalCy.elements().removeClass("selected related faded selected-edge");
    const selected = state.globalCy.$id(id);
    const related = selected.neighborhood();
    selected.addClass("selected");
    selected.union(related).addClass("related");
    state.globalCy.elements().not(selected.union(related)).addClass("faded");
  }
  setText("networkSelectionName", node.label || node.id);
  const kind = $("networkSelectionKind");
  kind.textContent = isFile ? "file" : (node.path ? "directory" : "aggregate");
  kind.style.display = "inline-flex";
  setText(
    "networkSelectionPath",
    node.path || "Aggregate of definitions outside the current directory scope",
  );
  setText(
    "networkMetrics",
    `${Number(node.files || 0).toLocaleString()} files · ${Number(node.symbols || 0).toLocaleString()} symbols\n`
    + `${Number(node.incoming || 0).toLocaleString()} incoming · ${Number(node.outgoing || 0).toLocaleString()} outgoing relationships\n`
    + `${Number(node.internal || 0).toLocaleString()} internal relationships`,
  );
  $("networkEnterBtn").disabled = !(node.can_drill || isFile);
  $("networkEnterBtn").textContent = isFile ? "Open indexed symbols" : "Explore module";
  setText(
    "networkEvidence",
    isFile
      ? "Open this file to inspect its compiler-indexed symbols and source locations."
      : "Select an edge to see the aggregated relationship count and direction.",
  );
}

function selectGlobalEdge(id) {
  const edge = state.globalNetwork && (state.globalNetwork.edges || []).find((item) => item.id === id);
  if (!edge) return;
  state.globalSelectedEdgeId = id;
  if (state.globalCy) {
    state.globalCy.edges().removeClass("selected-edge");
    state.globalCy.$id(id).addClass("selected-edge");
  }
  const source = globalNodeForId(edge.source);
  const target = globalNodeForId(edge.target);
  setText(
    "networkEvidence",
    `${source ? source.label : edge.source} → ${target ? target.label : edge.target} · ${edge.type} · ${Number(edge.relationships || 0).toLocaleString()} indexed relationships · aggregate weight ${Number(edge.weight || 0).toLocaleString()}`,
  );
}

function drawGlobalNetwork(network) {
  if (typeof window.cytoscape !== "function") {
    setText("networkCap", "Cytoscape could not load. Connect to the network or vendor the graph renderer for offline use.", "error");
    return;
  }
  if (state.globalCy) state.globalCy.destroy();
  state.globalCy = window.cytoscape({
    container: $("globalCy"),
    elements: globalCytoscapeElements(network),
    style: [
      { selector: "node", style: {
        "background-color": "data(color)", label: "data(label)", color: "#e6f0ff",
        "font-size": 10, "font-weight": 600, "text-valign": "bottom", "text-margin-y": 7,
        "text-outline-color": "#111b2d", "text-outline-width": 3,
        width: "data(nodeSize)", height: "data(nodeSize)",
        "border-width": 1.5, "border-color": "#dbeafe",
      } },
      { selector: "node.outside", style: { "border-style": "dashed", "border-color": "#cbd5e1" } },
      { selector: "node.file", style: { shape: "round-rectangle", "border-width": 2, "border-color": "#e0f2fe", "font-size": 9 } },
      { selector: "edge", style: {
        width: "data(lineWidth)", "line-color": "data(color)", "target-arrow-color": "data(color)",
        "target-arrow-shape": "triangle", "arrow-scale": .75, "curve-style": "bezier",
        "line-style": "data(lineStyle)", opacity: .78,
      } },
      { selector: ".related", style: { opacity: 1 } },
      { selector: ".faded", style: { opacity: .13 } },
      { selector: "node.selected", style: { "border-color": "#fbbf24", "border-width": 4 } },
      { selector: "edge.selected-edge", style: {
        width: 4.5, opacity: 1, label: "data(type)", color: "#f8fafc", "font-size": 10,
        "text-background-color": "#111b2d", "text-background-opacity": .9,
        "text-background-padding": 3, "text-outline-width": 0,
      } },
    ],
    wheelSensitivity: .16,
  });
  state.globalCy.on("tap", "node", (event) => { selectGlobalNode(event.target.id()); });
  state.globalCy.on("dbltap", "node", (event) => {
    const node = globalNodeForId(event.target.id());
    if (node && node.can_drill) enterGlobalScope(node.path);
    else if (isFileNetworkNode(node)) openFileSymbols(node.path);
  });
  state.globalCy.on("tap", "edge", (event) => { selectGlobalEdge(event.target.id()); });
  state.globalCy.on("tap", (event) => {
    if (event.target === state.globalCy) clearGlobalSelection();
  });
  runGlobalLayout();
}

async function loadGlobalNetwork(prefix = state.globalPrefix) {
  if (state.snapshotMode) return;
  const requestedPrefix = String(prefix || "").replace(/^\/+|\/+$/g, "");
  state.globalPrefix = requestedPrefix;
  setText("networkTitle", "Loading Linux code network…");
  try {
    const edgeTypes = selectedGlobalEdgeTypes();
    const params = new URLSearchParams({
      max_nodes: "100",
      max_edges: "320",
      include_internal: $("networkInternal").checked ? "1" : "0",
    });
    if (requestedPrefix) params.set("prefix", requestedPrefix);
    if (edgeTypes.length) params.set("edge_types", edgeTypes.join(","));
    const network = await api(`/api/global-network?${params.toString()}`);
    if (state.globalPrefix !== requestedPrefix) return;
    state.globalNetwork = network;
    setGlobalNetworkStatus(network);
    drawGlobalNetwork(network);
    clearGlobalSelection();
  } catch (error) {
    setText("networkTitle", error.message, "error");
    setText("networkStatus", "");
    setText("networkCap", "The global code network could not be loaded.", "error");
  }
}

function parentGlobalScope(prefix) {
  const parts = String(prefix || "").split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

async function enterGlobalScope(prefix) {
  if (!prefix) return;
  showTab("map");
  await loadGlobalNetwork(prefix);
}

function renderFileSymbolsMessage(message, className = "empty-state") {
  const list = $("fileSymbolsList");
  list.replaceChildren();
  const item = document.createElement("li");
  item.className = className;
  item.textContent = message;
  list.append(item);
}

function updateFileSymbolsMoreButton() {
  const button = $("fileSymbolsMoreBtn");
  const hasNext = state.fileSymbolsNextOffset !== null && state.fileSymbolsNextOffset !== undefined;
  button.hidden = !hasNext;
  button.disabled = !hasNext || state.fileSymbolsLoadingMore;
  button.textContent = state.fileSymbolsLoadingMore ? "Loading…" : "Load more indexed symbols";
}

function setFileSymbolsStatus(data) {
  const total = Number((data.totals || {}).symbols || 0);
  const loaded = state.fileSymbolsLoaded;
  const totalText = total ? ` of ${total.toLocaleString()}` : "";
  const more = state.fileSymbolsNextOffset !== null && state.fileSymbolsNextOffset !== undefined
    ? " · more available"
    : "";
  const nounCount = total || loaded;
  setText("fileSymbolsStatus", `${loaded.toLocaleString()}${totalText} indexed symbol${nounCount === 1 ? "" : "s"}${more}`);
}

function renderFileSymbols(data, append = false) {
  const list = $("fileSymbolsList");
  const symbols = data.symbols || [];
  if (!append) list.replaceChildren();
  if (!symbols.length && !append) {
    renderFileSymbolsMessage("No compiler-indexed symbols were recorded for this file.");
    return;
  }
  const path = (data.file && data.file.path) || state.fileSymbolsPath || "";
  for (const symbol of symbols) {
    const item = document.createElement("li");
    item.className = "file-symbol-item";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-symbol-button";
    button.textContent = symbol.name || "unnamed symbol";
    button.disabled = !symbol.scip_symbol;
    button.addEventListener("click", async () => {
      if (!symbol.scip_symbol) return;
      showTab("graph");
      await loadSymbol(symbol.scip_symbol);
    });
    const kind = document.createElement("span");
    kind.className = "kind-pill";
    kind.textContent = `${fileSymbolKindLabel(symbol.kind)}${symbol.is_external ? " · external" : ""}`;
    const signature = document.createElement("div");
    signature.className = "file-symbol-signature";
    signature.textContent = symbol.signature || "No signature recorded";
    const location = document.createElement("small");
    location.className = "file-symbol-location";
    location.textContent = fileLine(path, symbol.def_start_line);
    item.append(button, kind, signature, location);
    list.append(item);
  }
}

async function openFileSymbols(path) {
  const requestedPath = String(path || "").replace(/^\/+/, "");
  if (!requestedPath || state.snapshotMode) return;
  const request = ++state.fileSymbolsRequest;
  state.fileSymbolsPath = requestedPath;
  state.fileSymbolsNextOffset = null;
  state.fileSymbolsLoaded = 0;
  state.fileSymbolsLoadingMore = false;
  showTab("file");
  setText("filePath", requestedPath);
  setText("fileMeta", "Loading file metadata from the compiler index…");
  setText("fileSymbolsStatus", "Loading indexed symbols…");
  renderFileSymbolsMessage("Loading indexed symbols…");
  updateFileSymbolsMoreButton();
  try {
    const data = await api(`/api/file-symbols?path=${encodeURIComponent(requestedPath)}&limit=240`);
    if (request !== state.fileSymbolsRequest) return;
    const file = data.file || {};
    const actualPath = file.path || requestedPath;
    state.fileSymbolsPath = actualPath;
    setText("filePath", actualPath);
    const metadata = [file.language, file.subsystem].filter(Boolean);
    const indexedCount = Number((data.totals || {}).symbols || 0);
    if (indexedCount) metadata.push(`${indexedCount.toLocaleString()} indexed symbols`);
    setText("fileMeta", metadata.join(" · ") || "Compiler-indexed source file");
    state.fileSymbolsLoaded = (data.symbols || []).length;
    state.fileSymbolsNextOffset = data.next_offset ?? null;
    renderFileSymbols(data);
    setFileSymbolsStatus(data);
    updateFileSymbolsMoreButton();
  } catch (error) {
    if (request !== state.fileSymbolsRequest) return;
    setText("fileMeta", "The file-symbol endpoint did not return indexed symbols.");
    setText("fileSymbolsStatus", error.message, "error");
    renderFileSymbolsMessage(error.message, "error");
    updateFileSymbolsMoreButton();
  }
}

async function loadMoreFileSymbols() {
  const path = state.fileSymbolsPath;
  const offset = state.fileSymbolsNextOffset;
  if (!path || offset === null || offset === undefined || state.fileSymbolsLoadingMore) return;
  const request = state.fileSymbolsRequest;
  state.fileSymbolsLoadingMore = true;
  setText("fileSymbolsStatus", "Loading more indexed symbols…");
  updateFileSymbolsMoreButton();
  try {
    const data = await api(`/api/file-symbols?path=${encodeURIComponent(path)}&limit=240&offset=${encodeURIComponent(offset)}`);
    if (request !== state.fileSymbolsRequest) return;
    state.fileSymbolsLoaded += (data.symbols || []).length;
    state.fileSymbolsNextOffset = data.next_offset ?? null;
    renderFileSymbols(data, true);
    setFileSymbolsStatus(data);
  } catch (error) {
    if (request !== state.fileSymbolsRequest) return;
    setText("fileSymbolsStatus", error.message, "error");
  } finally {
    if (request === state.fileSymbolsRequest) {
      state.fileSymbolsLoadingMore = false;
      updateFileSymbolsMoreButton();
    }
  }
}

function drawGraph(fragment) {
  if (typeof window.cytoscape !== "function") {
    setText("cap", "Cytoscape could not load. Connect to the network or vendor the graph renderer for offline use.", "error");
    return;
  }
  if (state.cy) state.cy.destroy();
  state.cy = window.cytoscape({
    container: $("cy"),
    elements: cytoscapeElements(fragment),
    style: [
      { selector: "node", style: {
        "background-color": "data(color)", label: "data(name)", color: "#e6f0ff",
        "font-size": 10, "font-weight": 600, "text-valign": "bottom", "text-margin-y": 6,
        "text-outline-color": "#111b2d", "text-outline-width": 3, width: 32, height: 32,
        "border-width": 1.5, "border-color": "#dbeafe",
      } },
      { selector: "node.center", style: {
        width: 48, height: 48, "border-width": 3, "border-color": "#f8fafc",
        "background-color": "#2563eb", "font-size": 12,
      } },
      { selector: "edge", style: {
        width: 1.6, "line-color": "data(color)", "target-arrow-color": "data(color)",
        "target-arrow-shape": "triangle", "arrow-scale": .8, "curve-style": "bezier",
        "line-style": "data(lineStyle)", opacity: .82,
      } },
      { selector: ".related", style: { opacity: 1 } },
      { selector: ".faded", style: { opacity: .15 } },
      { selector: "node.selected", style: { "border-color": "#fbbf24", "border-width": 4 } },
      { selector: "edge.selected-edge", style: {
        width: 4, opacity: 1, label: "data(type)", color: "#f8fafc", "font-size": 10,
        "text-background-color": "#111b2d", "text-background-opacity": .88,
        "text-background-padding": 3, "text-outline-width": 0,
      } },
    ],
    wheelSensitivity: .16,
  });
  state.cy.on("tap", "node", (event) => { selectNode(event.target.id()); });
  state.cy.on("dbltap", "node", (event) => { loadSymbol(event.target.id()); });
  state.cy.on("tap", "edge", (event) => { selectEdge(event.target.id()); });
  state.cy.on("tap", (event) => {
    if (event.target === state.cy) clearGraphSelection();
  });
  runLayout();
}

function clearGraphSelection() {
  if (!state.cy) return;
  state.cy.elements().removeClass("selected related faded selected-edge");
  state.selectedEdgeId = null;
  setText("edgeEvidence", "Select an edge to see type, confidence, and observed source location.");
}

function renderSymbolList(id, rows) {
  const list = $(id);
  list.replaceChildren();
  if (!rows || !rows.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "—";
    list.append(item);
    return;
  }
  for (const row of rows.slice(0, 40)) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${row.name} · ${row.kind || "symbol"}${row.edge_type === "ops_bind" ? " · ops" : ""}`;
    button.addEventListener("click", () => {
      if (nodeForScip(row.scip_symbol)) selectNode(row.scip_symbol);
      else loadSymbol(row.scip_symbol);
    });
    const location = document.createElement("small");
    location.textContent = fileLine(row.file_path, row.line);
    item.append(button, location);
    list.append(item);
  }
}

function renderBody(body) {
  const container = $("body");
  container.replaceChildren();
  if (!body || !body.body) {
    container.textContent = body && body.note ? body.note : "No indexed on-disk body";
    container.className = "muted";
    return;
  }
  container.className = "";
  const location = document.createElement("div");
  location.className = "location";
  location.textContent = fileLine(body.file, body.start_line);
  const source = document.createElement("pre");
  source.textContent = body.body;
  container.append(location, source);
}

async function selectNode(scip) {
  const node = nodeForScip(scip);
  if (!node) return;
  state.selectedScip = scip;
  state.selectedEdgeId = null;
  if (state.cy) {
    state.cy.elements().removeClass("selected related faded selected-edge");
    const selected = state.cy.$id(scip);
    const related = selected.neighborhood();
    selected.addClass("selected");
    selected.union(related).addClass("related");
    state.cy.elements().not(selected.union(related)).addClass("faded");
  }
  setText("selectionName", displayName(node));
  const kind = $("selectionKind");
  kind.textContent = node.kind || "symbol";
  kind.style.display = "inline-flex";
  setText("selectionLocation", fileLine(node.def_file_path, node.def_start_line));
  $("focusBtn").disabled = scip === state.centeredScip;
  setText("edgeEvidence", "Select an edge to see type, confidence, and observed source location.");

  try {
    const [callers, callees, body] = await Promise.all([
      api(`/api/callers?scip=${encodeURIComponent(scip)}&depth=1&limit=40`),
      api(`/api/callees?scip=${encodeURIComponent(scip)}&depth=1&limit=40`),
      api(`/api/body?scip=${encodeURIComponent(scip)}`),
    ]);
    if (state.selectedScip !== scip) return;
    renderSymbolList("callers", callers);
    renderSymbolList("callees", callees);
    renderBody(body);
  } catch (error) {
    if (state.selectedScip === scip) setText("body", error.message, "error");
  }
}

function selectEdge(edgeId) {
  const edge = state.fragment && (state.fragment.edges || []).find((item) => item.id === edgeId);
  if (!edge) return;
  state.selectedEdgeId = edgeId;
  if (state.cy) {
    state.cy.edges().removeClass("selected-edge");
    state.cy.$id(edgeId).addClass("selected-edge");
  }
  const evidence = edge.evidence
    ? fileLine(edge.evidence.file_path, edge.evidence.line)
    : "no source location recorded";
  const field = edge.metadata && edge.metadata.field_name ? ` · ${edge.metadata.field_name}` : "";
  setText(
    "edgeEvidence",
    `${edge.type}${field} · confidence ${edge.confidence ?? "?"} · ${evidence}`,
  );
}

async function loadSymbol(scip) {
  if (!scip || state.snapshotMode) return;
  clearSearchResults();
  state.centeredScip = scip;
  setText("centerTag", "Loading graph fragment…");
  try {
    const depth = $("depth").value;
    const edgeTypes = selectedEdgeTypes();
    const params = new URLSearchParams({ scip, depth, max_nodes: "160", max_edges: "360" });
    if (edgeTypes.length) params.set("edge_types", edgeTypes.join(","));
    const fragment = await api(`/api/fragment?${params.toString()}`);
    state.fragment = fragment;
    const center = fragment.center || { scip_symbol: scip, name: scip, kind: "symbol" };
    $("q").value = center.name || "";
    setText("centerTag", `Centered on ${displayName(center)}`);
    setFragmentStatus(fragment);
    drawGraph(fragment);
    await selectNode(center.scip_symbol);
  } catch (error) {
    state.fragment = null;
    setText("centerTag", error.message, "error");
    setText("fragmentStatus", "");
    setText("cap", "The graph fragment could not be loaded.", "error");
  }
}

function renderOpsRows(rows) {
  const body = $("opsTable").querySelector("tbody");
  body.replaceChildren();
  if (!rows || !rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "muted";
    cell.textContent = "No matching bindings";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const result of rows) {
    const row = document.createElement("tr");
    row.className = "row";
    const values = [result.ops_name, result.impl_name, result.field_name || "—", fileLine(result.file_path, result.line), result.confidence ?? "—"];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    row.addEventListener("click", () => loadSymbol(result.impl_symbol));
    body.append(row);
  }
}

async function doOps() {
  const field = $("field").value.trim();
  if (!field || state.snapshotMode) return;
  setText("opsCount", "Resolving bindings…");
  try {
    const structType = $("st").value.trim();
    const rows = await api(`/api/ops?field=${encodeURIComponent(field)}${structType ? `&struct_type=${encodeURIComponent(structType)}` : ""}`);
    setText("opsCount", `${rows.length} implementation${rows.length === 1 ? "" : "s"}`);
    renderOpsRows(rows);
  } catch (error) {
    setText("opsCount", error.message, "error");
  }
}

function renderCallChain(chain) {
  const output = $("chainOut");
  output.replaceChildren();
  if (!chain || !chain.length) {
    output.textContent = "No caller path found.";
    output.className = "empty-state";
    return;
  }
  output.className = "";
  const list = document.createElement("ol");
  for (const step of chain) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tab";
    button.textContent = `${step.name} · ${step.kind || "symbol"}`;
    button.addEventListener("click", () => loadSymbol(step.scip_symbol));
    const location = document.createElement("span");
    location.className = "muted";
    location.textContent = ` ${step.depth ? "← called by at " : "· target"}${fileLine(step.file_path, step.line)}`;
    item.append(button, location);
    list.append(item);
  }
  output.append(list);
}

async function doChain() {
  const name = $("chainName").value.trim();
  if (!name || state.snapshotMode) return;
  setText("chainCount", "Tracing callers…");
  try {
    const results = await api(`/api/search?q=${encodeURIComponent(name)}&kind=function&limit=10`);
    const exact = rankCandidates(results, name).find((candidate) => candidate.name === name) || results[0];
    if (!exact) throw new Error(`No indexed function '${name}'`);
    const chain = await api(`/api/callchain?scip=${encodeURIComponent(exact.scip_symbol)}&max_depth=20`);
    setText("chainCount", `${chain.length} level${chain.length === 1 ? "" : "s"}`);
    renderCallChain(chain);
  } catch (error) {
    setText("chainCount", error.message, "error");
  }
}

function showTab(tab) {
  const panes = { map: "paneMap", file: "paneFile", graph: "paneGraph", ops: "paneOps", chain: "paneChain" };
  for (const [key, id] of Object.entries(panes)) {
    $(id).style.display = key === tab ? ((key === "graph" || key === "map") ? "grid" : "block") : "none";
  }
  $("tabMap").classList.toggle("active", tab === "map");
  $("tabFile").classList.toggle("active", tab === "file");
  $("tabGraph").classList.toggle("active", tab === "graph");
  $("tabOps").classList.toggle("active", tab === "ops");
  $("tabChain").classList.toggle("active", tab === "chain");
  if (tab === "graph" && state.cy) setTimeout(() => state.cy.resize(), 0);
  if (tab === "map" && state.globalCy) setTimeout(() => state.globalCy.resize(), 0);
}

function snapshotBody(body) {
  const wrapper = document.createElement("div");
  if (!body || !body.body) {
    wrapper.textContent = "No source body";
    return wrapper;
  }
  const loc = document.createElement("p");
  loc.className = "muted";
  loc.textContent = fileLine(body.file, body.start_line);
  const source = document.createElement("pre");
  source.textContent = body.body;
  wrapper.append(loc, source);
  return wrapper;
}

function snapshotTable(rows, columns) {
  const table = document.createElement("table");
  const body = document.createElement("tbody");
  for (const result of rows || []) {
    const row = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("td");
      cell.textContent = column(result);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(body);
  return table;
}

async function loadSnapshot(key) {
  const snapshot = state.snapshots && state.snapshots[key];
  if (!snapshot) return;
  const output = $("demoOut");
  output.replaceChildren();
  try {
    const data = await api(snapshot.file);
    setText("demoTitle", `(${snapshot.count} items)`);
    if (snapshot.view === "ops") {
      output.append(snapshotTable(data, [
        (row) => row.ops_name, (row) => row.impl_name, (row) => row.field_name || "",
        (row) => fileLine(row.file_path, row.line), (row) => row.confidence ?? "",
      ]));
    } else if (snapshot.view === "callchain") {
      renderCallChain(data);
      output.append($("chainOut").cloneNode(true));
    } else if (snapshot.view === "body") {
      output.append(snapshotBody(data));
    } else if (snapshot.view === "struct") {
      output.append(snapshotTable(data.fields || [], [
        (field) => field.name, (field) => field.kind, (field) => field.signature || "",
      ]));
    }
  } catch (error) {
    output.textContent = error.message;
    output.className = "error";
  }
}

async function enterSnapshotMode() {
  state.snapshotMode = true;
  document.querySelectorAll("[data-live]").forEach((element) => { element.style.display = "none"; });
  $("demoPanel").style.display = "block";
  setText("dbtag", "static demo (Pages)");
  try {
    const manifest = await api("data/manifest.json");
    state.snapshots = Object.fromEntries(manifest.map((item) => [item.key, item]));
    const select = $("demoSelect");
    select.replaceChildren();
    for (const item of manifest) {
      const option = document.createElement("option");
      option.value = item.key;
      option.textContent = item.title;
      select.append(option);
    }
    select.addEventListener("change", () => loadSnapshot(select.value));
    if (manifest[0]) loadSnapshot(manifest[0].key);
  } catch (error) {
    setText("demoOut", `No snapshots available: ${error.message}`, "error");
  }
}

async function detectMode() {
  try {
    const status = await api("/api/status");
    const symbols = (status.metadata || {}).total_symbols || "?";
    setText("dbtag", `kgraph.db · ${symbols} symbols`);
    await loadGlobalNetwork();
  } catch {
    await enterSnapshotMode();
  }
}

function setDemoCaption(zh, en) {
  setText("demoZh", zh);
  setText("demoEn", en);
}

function clearDemoTimers() {
  state.demoTimers.forEach((timer) => clearTimeout(timer));
  state.demoTimers = [];
}

async function findDemoSymbol() {
  const candidates = await api("/api/search?q=vfs_read&kind=function&limit=10");
  const exact = rankCandidates(candidates, "vfs_read").find((candidate) => candidate.name === "vfs_read");
  if (!exact) throw new Error("The demo symbol vfs_read was not found in this index.");
  state.demoSymbol = exact.scip_symbol;
  return state.demoSymbol;
}

function setDemoEdgeTypes(types) {
  document.querySelectorAll("#edgeTypes input").forEach((input) => {
    input.checked = types.includes(input.value);
  });
}

function scheduleDemo(atSeconds, zh, en, action) {
  const timer = setTimeout(async () => {
    setDemoCaption(zh, en);
    try {
      await action();
    } catch (error) {
      setDemoCaption("演示数据加载失败", `Demo data failed to load: ${error.message}`);
    }
  }, atSeconds * 1000);
  state.demoTimers.push(timer);
}

function startVideoDemo() {
  clearDemoTimers();
  const start = $("startDemoBtn");
  start.disabled = true;
  start.textContent = "Playing…";
  setDemoEdgeTypes(["calls", "ops_bind"]);
  $("depth").value = "1";
  $("layout").value = "force";
  showTab("map");
  scheduleDemo(0,
    "全局 Linux 代码网络 · 47 万+ 符号", "Global Linux code network · 479K+ symbols",
    async () => loadGlobalNetwork());
  scheduleDemo(5,
    "下钻 fs：真实跨目录依赖", "Drill into fs: real cross-directory dependencies",
    async () => enterGlobalScope("fs"));
  scheduleDemo(10,
    "从全局网络进入 vfs_read", "Enter vfs_read from the global network",
    async () => { showTab("graph"); await loadSymbol(await findDemoSymbol()); });
  scheduleDemo(15,
    "两跳上下文：真实有向 calls 与 ops_bind 边", "Two-hop context: real directed calls and ops_bind edges",
    async () => { $("depth").value = "2"; await loadSymbol(state.demoSymbol); });
  scheduleDemo(20,
    "函数指针：read_iter 的真实实现", "Function pointers: real read_iter implementations",
    async () => { showTab("ops"); $("field").value = "read_iter"; await doOps(); });
  scheduleDemo(25,
    "关系证据与源码位置，来自编译器索引", "Relationship evidence and source locations from the compiler index",
    async () => {
      showTab("graph");
      $("depth").value = "1";
      $("layout").value = "rings";
      await loadSymbol(state.demoSymbol);
    });
  const done = setTimeout(() => {
    setDemoCaption("KGraph · 编译器感知的代码图谱探索", "KGraph · compiler-aware code graph exploration");
    start.disabled = false;
    start.textContent = "Replay demo";
  }, 30_000);
  state.demoTimers.push(done);
}

function configureVideoMode() {
  if (!VIDEO_MODE) return;
  document.body.classList.add("video-mode");
  $("videoDemo").classList.add("visible");
  $("startDemoBtn").addEventListener("click", startVideoDemo);
}

$("searchBtn").addEventListener("click", doSearch);
$("q").addEventListener("keydown", (event) => { if (event.key === "Enter") doSearch(); });
$("depth").addEventListener("change", () => { if (state.centeredScip) loadSymbol(state.centeredScip); });
$("layout").addEventListener("change", runLayout);
$("fitBtn").addEventListener("click", () => { if (state.cy) state.cy.fit(undefined, 42); });
document.querySelectorAll("#edgeTypes input").forEach((input) => input.addEventListener("change", () => {
  if (state.centeredScip) loadSymbol(state.centeredScip);
}));
$("focusBtn").addEventListener("click", () => { if (state.selectedScip) loadSymbol(state.selectedScip); });
$("tabMap").addEventListener("click", () => showTab("map"));
$("tabFile").addEventListener("click", () => showTab("file"));
$("tabGraph").addEventListener("click", () => showTab("graph"));
$("tabOps").addEventListener("click", () => showTab("ops"));
$("tabChain").addEventListener("click", () => showTab("chain"));
$("opsBtn").addEventListener("click", doOps);
$("field").addEventListener("keydown", (event) => { if (event.key === "Enter") doOps(); });
$("chainBtn").addEventListener("click", doChain);
$("chainName").addEventListener("keydown", (event) => { if (event.key === "Enter") doChain(); });
$("networkLayout").addEventListener("change", runGlobalLayout);
$("networkFitBtn").addEventListener("click", () => { if (state.globalCy) state.globalCy.fit(undefined, 52); });
$("networkBackBtn").addEventListener("click", () => loadGlobalNetwork(parentGlobalScope(state.globalPrefix)));
$("networkEnterBtn").addEventListener("click", () => {
  const node = globalNodeForId(state.globalSelectedGroup);
  if (node && node.can_drill) enterGlobalScope(node.path);
  else if (isFileNetworkNode(node)) openFileSymbols(node.path);
});
$("fileSymbolsMoreBtn").addEventListener("click", loadMoreFileSymbols);
$("networkInternal").addEventListener("change", () => loadGlobalNetwork());
document.querySelectorAll("#globalEdgeTypes input").forEach((input) => input.addEventListener("change", () => {
  loadGlobalNetwork();
}));

configureVideoMode();
detectMode();
