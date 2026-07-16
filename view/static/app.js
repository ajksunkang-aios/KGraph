// KGraph Health dashboard — renders the last 7 runs from data/metrics.jsonl.
// Static, dependency-free. Serve over HTTP (GitHub Pages or `python -m http.server`).
"use strict";

const ROWS = 7;

function fmtNum(n) {
  if (typeof n !== "number") return "—";
  return n.toLocaleString("en-US");
}
function fmtDur(buildS, scipS, ingestS) {
  const f = (x) => (typeof x === "number" && x > 0) ? Math.round(x) : "—";
  return `${f(buildS)} / ${f(scipS)} / ${f(ingestS)}`;
}
function pill(ok, text) {
  return `<span class="pill ${ok ? "ok" : "bad"}">${text}</span>`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function render(runs) {
  const cards = document.getElementById("cards");
  const tbody = document.getElementById("rows");

  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="none">No runs yet. Trigger
      <span class="mono">Linux Build &amp; Index Probe</span> to populate.</td></tr>`;
    cards.innerHTML = "";
    return;
  }

  const last = runs[runs.length - 1];
  const last7 = runs.slice(-ROWS).reverse();
  const buildable = last7.filter((r) => r.buildable).length;
  const benched = last7.filter((r) => r.benchmark_ok).length;

  cards.innerHTML = [
    card("Latest HEAD", `<span class="mono">${esc(last.head || "—")}</span>`),
    card("Buildable (7d)", `${buildable}/${last7.length}`),
    card("Benchmark OK (7d)", `${benched}/${last7.length}`),
    card("Latest symbols", fmtNum(last.symbols)),
  ].join("");

  tbody.innerHTML = last7.map((r) => `
    <tr>
      <td class="l">${esc((r.ts || "").replace("T", " ").slice(0, 19))}</td>
      <td class="l mono">${esc(r.head || "—")}</td>
      <td>${pill(r.buildable, r.buildable ? "OK" : "FAIL")}</td>
      <td>${pill(r.benchmark_ok, `${r.benchmark_pass}/${r.benchmark_total}`)}</td>
      <td class="r">${fmtNum(r.symbols)}</td>
      <td class="r">${fmtNum(r.edges)}</td>
      <td class="r">${fmtNum(r.ops_bind)}</td>
      <td class="r">${fmtNum(r.contains)}</td>
      <td class="r">${r.db_mb ?? "—"}</td>
      <td class="r">${fmtDur(r.build_s, r.scip_s, r.ingest_s)}</td>
    </tr>`).join("");
}

function card(k, v) {
  return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
}

async function main() {
  try {
    const res = await fetch("data/metrics.jsonl", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const text = await res.text();
    const runs = text.split("\n").map((l) => l.trim())
      .filter(Boolean).map((l) => JSON.parse(l));
    render(runs);
  } catch (e) {
    document.getElementById("rows").innerHTML =
      `<tr><td colspan="10" class="none">Could not load
      <span class="mono">data/metrics.jsonl</span> (${esc(e.message)}).
      Serve this folder over HTTP, e.g. <span class="mono">python3 -m http.server</span>.</td></tr>`;
  }
}

main();
