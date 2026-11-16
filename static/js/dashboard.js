// Screen 2 (#/dashboard): KPI tiles with 14-day sparklines, the exception
// queue (largest first), decisions by reason code, and the full run history
// with server-side filters, sort and paging.

import { api } from "./api.js";
import { clear, codeChip, h, icon, showError, verdictChip, when } from "./ui.js";

const VERDICT_OPTIONS = [["", "All verdicts"], ["AUTO_APPROVE", "Auto-approved"], ["APPROVE_PENDING_SIGNOFF", "Awaiting sign-off"],
                         ["HOLD_FOR_REVIEW", "Held for review"], ["REJECT", "Rejected"], ["BLOCK", "Blocked"]];

export function mountDashboard(root) {
  clear(root);
  document.title = "Dashboard — Invoice Decisioning";
  const state = { verdict: "", vendor: "", date_from: "", date_to: "", q: "", sort: "created_at", order: "desc", page: 1 };

  const tiles = h("div", { class: "grid grid-cols-3 gap-4 xl:grid-cols-6" });
  const queueBody = h("div");
  const queueCount = h("span", { class: "mono text-sm text-zinc-500" });
  const reasons = h("div");
  const tableHost = h("div");
  const pager = h("div", { class: "flex items-center justify-between border-t border-zinc-100 px-6 py-3 text-sm text-zinc-600" });
  const subtitle = h("p", { class: "mt-1 text-sm text-zinc-500" });

  root.append(
    h("div", { class: "mb-6" }, h("h1", { class: "text-xl font-semibold tracking-tight" }, "Dashboard"), subtitle),
    tiles,
    h("div", { class: "mt-6 grid grid-cols-1 gap-6 xl:grid-cols-3" },
      h("section", { class: "rounded-xl border border-zinc-200 bg-white xl:col-span-2" },
        h("div", { class: "flex items-baseline justify-between px-6 pt-5 pb-3" },
          h("h2", { class: "text-sm font-semibold text-zinc-900" }, "Needs a person"), queueCount),
        queueBody),
      h("section", { class: "rounded-xl border border-zinc-200 bg-white p-6" },
        h("h2", { class: "text-sm font-semibold text-zinc-900" }, "Decisions by reason code"),
        h("p", { class: "mt-1 text-xs text-zinc-500" }, "All runs. A run can carry several reasons."),
        reasons)),
    h("section", { class: "mt-6 rounded-xl border border-zinc-200 bg-white" },
      h("div", { class: "flex items-baseline justify-between px-6 pt-5" },
        h("h2", { class: "text-sm font-semibold text-zinc-900" }, "All runs")),
      filters(state, () => { state.page = 1; loadRuns(); }),
      tableHost, pager));

  async function loadSummary() {
    try {
      const [stats, queue] = await Promise.all([api.stats(), api.runs({ queue: "true" })]);
      subtitle.textContent = `Today is ${stats.today} (IST). Each tile shows today, with the last ${stats.days.length} days behind it.`;
      clear(tiles);
      for (const t of stats.tiles) tiles.appendChild(tile(t, stats.days));
      queueCount.textContent = `${stats.queue_count} open`;
      clear(queueBody).appendChild(queue.rows.length ? queueTable(queue.rows)
        : h("p", { class: "px-6 pb-6 text-sm text-zinc-500" }, "Nothing is waiting for a person. Every decided invoice is approved or resolved."));
      clear(reasons).appendChild(reasonBars(stats.reasons));
    } catch (err) {
      showError(`Could not load the dashboard: ${err.message}`);
    }
  }

  async function loadRuns() {
    const params = Object.fromEntries(Object.entries(state).filter(([, v]) => v !== "" && v !== null));
    try {
      const res = await api.runs(params);
      clear(tableHost).appendChild(runsTable(res.rows, state, (key) => {
        state.order = state.sort === key && state.order === "desc" ? "asc" : "desc";
        state.sort = key;
        loadRuns();
      }));
      const first = res.total ? (res.page - 1) * res.page_size + 1 : 0;
      const last = Math.min(res.total, res.page * res.page_size);
      clear(pager).append(
        h("span", { class: "mono" }, res.total ? `${first}–${last} of ${res.total.toLocaleString("en-IN")}` : "0 runs"),
        h("div", { class: "flex gap-2" },
          pageBtn("Previous", res.page <= 1, () => { state.page -= 1; loadRuns(); }),
          pageBtn("Next", last >= res.total, () => { state.page += 1; loadRuns(); })));
    } catch (err) {
      showError(`Could not load runs: ${err.message}`);
    }
  }

  loadSummary();
  loadRuns();
}

function pageBtn(label, disabled, onclick) {
  return h("button", { type: "button", disabled, onclick,
                       class: "focus-ring rounded-lg border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-40" }, label);
}

// ---------------------------------------------------------------- KPI tiles

function tile(t, days) {
  return h("div", { class: "rounded-xl border border-zinc-200 bg-white px-4 pt-4 pb-3" },
    h("p", { class: "text-xs text-zinc-500" }, t.label),
    h("p", { class: "mono mt-1 truncate text-2xl tracking-tight text-zinc-900", title: t.display }, t.display),
    sparkline(t.series, days, t));
}

// One series, one neutral line; today's point carries the marker. Hovering a
// day shows its value.
function sparkline(series, days, t) {
  const W = 160, H = 34, P = 3;
  const vals = series.map((v) => (v === null ? null : Number(v)));
  const present = vals.filter((v) => v !== null);
  const max = Math.max(...present, 0), min = Math.min(...present, 0);
  const x = (i) => P + (i * (W - 2 * P)) / (series.length - 1);
  const y = (v) => H - P - ((v - min) / (max - min || 1)) * (H - 2 * P);
  const segs = [];
  let cur = [];
  vals.forEach((v, i) => { if (v === null) { if (cur.length) segs.push(cur); cur = []; } else cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`); });
  if (cur.length) segs.push(cur);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "mt-2 block h-9 w-full overflow-visible");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${t.label}, last ${series.length} days: ${vals.map((v) => (v === null ? "none" : v)).join(", ")}`);
  const base = document.createElementNS(ns, "line");
  Object.entries({ x1: P, x2: W - P, y1: H - P, y2: H - P, stroke: "#e4e4e7", "stroke-width": 1 }).forEach(([k, v]) => base.setAttribute(k, v));
  svg.appendChild(base);
  for (const pts of segs) {
    const pl = document.createElementNS(ns, "polyline");
    Object.entries({ points: pts.join(" "), fill: "none", stroke: "#71717a", "stroke-width": 1.75, "stroke-linejoin": "round", "stroke-linecap": "round" })
      .forEach(([k, v]) => pl.setAttribute(k, v));
    svg.appendChild(pl);
  }
  vals.forEach((v, i) => {
    // generous invisible hit target per day, with a native tooltip
    const hit = document.createElementNS(ns, "rect");
    Object.entries({ x: x(i) - (W / series.length) / 2, y: 0, width: W / series.length, height: H, fill: "transparent" })
      .forEach(([k, val]) => hit.setAttribute(k, val));
    const tt = document.createElementNS(ns, "title");
    tt.textContent = `${days[i]}: ${v === null ? "no runs" : formatLike(t, v)}`;
    hit.appendChild(tt);
    svg.appendChild(hit);
  });
  const lastI = vals.length - 1;
  if (vals[lastI] !== null) {
    const dot = document.createElementNS(ns, "circle");
    Object.entries({ cx: x(lastI), cy: y(vals[lastI]), r: 3, fill: "#18181b", stroke: "#fff", "stroke-width": 1.5 })
      .forEach(([k, v]) => dot.setAttribute(k, v));
    svg.appendChild(dot);
  }
  return svg;
}

function formatLike(t, v) {
  if (t.key === "auto_rate") return `${Math.round(v * 100)}%`;
  if (t.key === "value_auto") return `₹${Math.floor(v / 100).toLocaleString("en-IN")}`;
  if (t.key === "median_ms") return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`;
  return v.toLocaleString("en-IN");
}

// ---------------------------------------------------------------- reason codes

function reasonBars(rows) {
  if (!rows.length) return h("p", { class: "mt-4 text-sm text-zinc-500" }, "No exceptions recorded yet.");
  const max = Math.max(...rows.map((r) => r.count));
  return h("ul", { class: "mt-4 space-y-2.5" }, rows.map((r) =>
    h("li", { class: "group", title: `${r.code}: ${r.count} run${r.count === 1 ? "" : "s"}` },
      h("div", { class: "flex items-baseline justify-between gap-2 text-xs" },
        h("span", { class: "mono truncate text-zinc-700" }, r.code),
        h("span", { class: "mono text-zinc-900" }, String(r.count))),
      h("div", { class: "mt-1 h-2 rounded-r bg-zinc-100" },
        h("div", { class: "h-2 rounded-r bg-zinc-500 transition-colors group-hover:bg-zinc-700",
                   style: { width: `${Math.max(2, (r.count / max) * 100)}%` } })))));
}

// ---------------------------------------------------------------- tables

function fileCell(r) {
  const ext = (r.source_filename || "").split(".").pop().toUpperCase().slice(0, 4);
  return h("span", { class: "flex min-w-0 items-center gap-2" },
    icon(r.mime === "application/pdf" ? "file" : "image", "text-zinc-400"),
    h("span", { class: "truncate", title: r.source_filename }, r.source_filename),
    h("span", { class: "mono shrink-0 rounded bg-zinc-100 px-1 text-[10px] text-zinc-500" }, ext));
}

function verdictCell(r) {
  if (r.status === "running" || r.status === "queued") return h("span", { class: "text-xs text-blue-600" }, "Running");
  return h("span", { class: "flex items-center gap-1.5" }, verdictChip(r.decision),
    r.resolution ? h("span", { class: "text-xs text-zinc-500", title: `${r.resolution.action === "override_approve" ? "Approved" : "Rejected"} by ${r.resolution.actor} at ${r.resolution.at}` },
      r.resolution.action === "override_approve" ? "· approved by hand" : "· rejected by hand") : null);
}

const go = (id) => { location.hash = `#/run/${encodeURIComponent(id)}`; };

function rowAttrs(r) {
  return { class: "cursor-pointer hover:bg-zinc-50 focus-ring", tabindex: "0", onclick: () => go(r.id),
           onkeydown: (e) => { if (e.key === "Enter") go(r.id); } };
}

function queueTable(rows) {
  return h("table", { class: "w-full table-fixed text-left text-sm" },
    h("thead", { class: "border-y border-zinc-100 bg-zinc-50 text-xs text-zinc-500" },
      h("tr", {},
        h("th", { class: "w-36 px-6 py-2 text-right font-medium" }, "Amount"),
        h("th", { class: "px-3 py-2 font-medium" }, "Vendor · invoice"),
        h("th", { class: "w-40 px-3 py-2 font-medium" }, "Verdict"),
        h("th", { class: "px-3 py-2 font-medium" }, "Why"),
        h("th", { class: "w-28 px-6 py-2 font-medium" }, "Received"))),
    h("tbody", { class: "divide-y divide-zinc-100" }, rows.map((r) =>
      h("tr", rowAttrs(r),
        h("td", { class: "mono px-6 py-2.5 text-right text-zinc-900" }, r.total_display || "—"),
        h("td", { class: "px-3 py-2.5" },
          h("p", { class: "truncate text-zinc-900" }, r.vendor_name || "Unknown vendor"),
          h("p", { class: "mono truncate text-xs text-zinc-500" }, r.invoice_number || r.source_filename)),
        h("td", { class: "px-3 py-2.5" }, verdictChip(r.decision)),
        h("td", { class: "px-3 py-2.5" }, h("div", { class: "flex flex-wrap gap-1" },
          r.reasons.slice(0, 3).map((c) => codeChip(c.split(":")[0], ["BLOCK", "REJECT"].includes(r.decision) ? "rose" : "amber")))),
        h("td", { class: "px-6 py-2.5 text-xs text-zinc-500" }, when(r.created_at))))));
}

function runsTable(rows, state, onSort) {
  if (!rows.length) {
    return h("p", { class: "px-6 py-8 text-center text-sm text-zinc-500" },
      "No runs match these filters. Clear a filter, or process an invoice from the Run screen.");
  }
  const sortable = (key, label, cls = "") => {
    const on = state.sort === key;
    return h("th", { class: `px-3 py-2 font-medium ${cls}`, "aria-sort": on ? (state.order === "asc" ? "ascending" : "descending") : "none" },
      h("button", { type: "button", class: `focus-ring inline-flex items-center gap-1 rounded ${on ? "text-zinc-900" : ""}`, onclick: () => onSort(key) },
        label, on ? (state.order === "asc" ? " ↑" : " ↓") : ""));
  };
  return h("div", { class: "overflow-x-auto" }, h("table", { class: "w-full min-w-[1180px] table-fixed text-left text-sm" },
    h("thead", { class: "border-y border-zinc-100 bg-zinc-50 text-xs text-zinc-500" },
      h("tr", {},
        sortable("created_at", "Time", "w-28 pl-6"),
        h("th", { class: "w-60 px-3 py-2 font-medium" }, "File"),
        h("th", { class: "px-3 py-2 font-medium" }, "Vendor"),
        h("th", { class: "w-36 px-3 py-2 font-medium" }, "Invoice no."),
        sortable("amount", "Amount", "w-32 text-right"),
        h("th", { class: "w-24 px-3 py-2 font-medium" }, "PO"),
        h("th", { class: "w-48 px-3 py-2 font-medium" }, "Verdict"),
        sortable("confidence", "Conf.", "w-16 text-right"),
        sortable("duration", "Duration", "w-24 pr-6 text-right"))),
    h("tbody", { class: "divide-y divide-zinc-100" }, rows.map((r) =>
      h("tr", rowAttrs(r),
        h("td", { class: "whitespace-nowrap py-2.5 pl-6 pr-3 text-xs text-zinc-500" }, when(r.created_at)),
        h("td", { class: "px-3 py-2.5 text-zinc-800" }, fileCell(r)),
        h("td", { class: "truncate px-3 py-2.5 text-zinc-800", title: r.vendor_name || "" }, r.vendor_name || "—"),
        h("td", { class: "mono truncate px-3 py-2.5 text-zinc-800" }, r.invoice_number || "—"),
        h("td", { class: "mono px-3 py-2.5 text-right text-zinc-900" }, r.total_display || "—"),
        h("td", { class: "mono px-3 py-2.5 text-zinc-700" }, r.matched_po_number || "—"),
        h("td", { class: "px-3 py-2.5" }, verdictCell(r)),
        h("td", { class: "mono px-3 py-2.5 text-right text-zinc-700" }, r.confidence != null ? r.confidence.toFixed(2) : "—"),
        h("td", { class: "mono py-2.5 pl-3 pr-6 text-right text-zinc-700" },
          r.duration_ms == null ? "—" : r.duration_ms >= 1000 ? `${(r.duration_ms / 1000).toFixed(1)} s` : `${r.duration_ms} ms`))))));
}

// ---------------------------------------------------------------- filters

function filters(state, onChange) {
  const cls = "focus-ring rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-800";
  const verdict = h("select", { class: cls, "aria-label": "Verdict", onchange: (e) => { state.verdict = e.target.value; onChange(); } },
    VERDICT_OPTIONS.map(([v, l]) => h("option", { value: v }, l)));
  const vendor = h("select", { class: cls, "aria-label": "Vendor", onchange: (e) => { state.vendor = e.target.value; onChange(); } },
    h("option", { value: "" }, "All vendors"));
  api.vendors().then((vs) => vs.forEach((v) => vendor.appendChild(h("option", { value: v.id }, `${v.legal_name} (${v.id})`))))
    .catch((err) => showError(`Could not load vendors: ${err.message}`));
  const from = h("input", { type: "date", class: cls, "aria-label": "From date", onchange: (e) => { state.date_from = e.target.value; onChange(); } });
  const to = h("input", { type: "date", class: cls, "aria-label": "To date", onchange: (e) => { state.date_to = e.target.value; onChange(); } });
  let timer = null;
  const search = h("input", { type: "search", class: `${cls} min-w-0 flex-1`, placeholder: "Search file name, invoice number, PO or run ID",
                              "aria-label": "Search",
                              oninput: (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value; onChange(); }, 250); } });
  return h("div", { class: "flex flex-wrap items-center gap-2 px-6 py-4" }, search, verdict, vendor,
    h("span", { class: "flex items-center gap-1.5 text-xs text-zinc-500" }, from, "to", to));
}
