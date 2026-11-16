// Screen 1 (#/run): intake (dropzone + sample chips) and the live workspace.
// The workspace (document viewer, stage timeline over SSE, decision card) is
// exported so the run-detail screen renders exactly the same thing.

import { api } from "./api.js";
import {
  STAGE_STATUS, VERDICT, clear, codeChip, confidenceBar, h, icon, money, ms, ruleChip, showError,
} from "./ui.js";
import { createViewer } from "./viewer.js";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff";

// ================================================================ screen

export function mountRun(root, { runId } = {}) {
  clear(root);
  let workspace = null;
  const intake = buildIntake({ compact: Boolean(runId) });
  root.appendChild(intake);

  if (runId) {
    workspace = createWorkspace(runId);
    root.appendChild(workspace.el);
    workspace.start();
  } else {
    document.title = "Invoice Decisioning";
    root.appendChild(h("p", { class: "mt-10 text-center text-sm text-zinc-500" },
      "Drop an invoice or choose a sample to watch it being read, matched and decided, stage by stage."));
  }
  return () => workspace && workspace.destroy();
}

function go(runId) {
  location.hash = `#/run?id=${encodeURIComponent(runId)}`;
}

function buildIntake({ compact }) {
  const input = h("input", { type: "file", accept: ACCEPT, class: "sr-only", id: "file-input" });
  const status = h("p", { class: "mt-1 text-sm text-zinc-500" }, "PDF, PNG, JPEG or TIFF · up to 25 MB · 15 pages");
  const zone = h("label", {
    for: "file-input", tabindex: "0", role: "button", "aria-label": "Upload an invoice",
    class: `dropzone focus-ring flex cursor-pointer items-center justify-center gap-4 rounded-xl border-2 border-dashed border-zinc-300 bg-white text-center transition-colors hover:border-indigo-400 ${compact ? "px-6 py-5" : "px-6 py-12"}`,
  },
    h("span", { class: "flex h-10 w-10 items-center justify-center rounded-full bg-zinc-100 text-zinc-600" }, icon("upload")),
    h("span", { class: "text-left" },
      h("span", { class: "block font-medium text-zinc-900" },
        "Drop an invoice here, or ", h("span", { class: "text-indigo-600 underline-offset-2 hover:underline" }, "browse")),
      status),
  );

  let busy = false;
  async function send(file) {
    if (!file || busy) return;
    busy = true;
    status.textContent = `Uploading ${file.name}…`;
    try {
      const { run_id } = await api.upload(file);
      go(run_id);
    } catch (err) {
      showError(`Upload failed: ${err.message}`);
      status.textContent = "PDF, PNG, JPEG or TIFF · up to 25 MB · 15 pages";
    } finally {
      busy = false;
      input.value = "";
    }
  }

  input.addEventListener("change", () => send(input.files[0]));
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  for (const ev of ["dragenter", "dragover"]) zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("dropzone-over"); });
  for (const ev of ["dragleave", "drop"]) zone.addEventListener(ev, () => zone.classList.remove("dropzone-over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); send(e.dataTransfer.files[0]); });

  const chips = h("div", { class: "mt-4 flex flex-wrap items-center gap-2" },
    h("span", { class: "mr-1 text-xs font-medium uppercase tracking-wide text-zinc-500" }, "Samples"));
  api.samples().then((list) => {
    for (const s of list) {
      const chip = h("button", {
        type: "button", disabled: !s.available,
        title: s.available ? `Run ${s.filename}` : `${s.filename} has not been generated yet`,
        class: "focus-ring inline-flex items-center gap-1.5 rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-700 transition-colors hover:border-zinc-300 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-40",
        onclick: async () => {
          chip.disabled = true;
          try {
            const { run_id } = await api.fromSample(s.key);
            go(run_id);
          } catch (err) {
            showError(`Could not start ${s.label}: ${err.message}`);
          } finally {
            chip.disabled = !s.available;
          }
        },
      }, icon(s.filename.endsWith(".pdf") ? "file" : "image", "text-zinc-400"), s.label);
      chips.appendChild(chip);
    }
  }).catch((err) => showError(`Could not load the sample list: ${err.message}`));

  return h("section", {}, input, zone, chips);
}

// ================================================================ workspace

export function createWorkspace(runId, { onRecord } = {}) {
  const rows = new Map();
  let es = null;
  let ticker = null;
  let record = null;
  let filename = "";

  const viewer = createViewer({ onSelect: revealField });
  const cardSlot = h("div", { class: "space-y-6" });
  const conn = h("span", { class: "text-xs text-zinc-400" });
  const list = h("ol", { class: "divide-y divide-zinc-100" });
  const timeline = h("section", { class: "rounded-xl border border-zinc-200 bg-white" },
    h("div", { class: "flex items-center justify-between border-b border-zinc-100 px-6 py-4" },
      h("h2", { class: "text-sm font-medium text-zinc-900" }, "Pipeline"), conn),
    list);
  const extras = h("div", { class: "space-y-6" });

  const el = h("div", { class: "mt-8 grid grid-cols-5 gap-6" },
    h("div", { class: "col-span-2" }, h("div", { class: "sticky top-6" }, viewer.el)),
    h("div", { class: "col-span-3 space-y-6" }, cardSlot, timeline, extras));

  // ---------------------------------------------------------- stage rows

  function ensureRow(stage) {
    if (rows.has(stage.stage_key)) return rows.get(stage.stage_key);
    const dot = h("span", { class: "h-2.5 w-2.5 rounded-full" });
    const word = h("span", { class: "w-16 text-xs font-medium" });
    const elapsed = h("span", { class: "mono w-16 shrink-0 text-right text-xs text-zinc-500" });
    const summary = h("span", { class: "line-clamp-2 min-w-0 flex-1 text-sm text-zinc-600" });
    const chevron = icon("chevron", "text-zinc-300 transition-transform duration-150");
    const head = h("button", {
      type: "button", class: "focus-ring flex w-full items-center gap-3 px-6 py-3 text-left disabled:cursor-default",
      "aria-expanded": "false",
    },
      h("span", { class: "flex w-24 shrink-0 items-center gap-2" }, dot, word),
      h("span", { class: "mono w-6 shrink-0 text-xs text-zinc-400" }, String(stage.seq).padStart(2, "0")),
      h("span", { class: "w-24 shrink-0 text-sm font-medium text-zinc-900" }, stage.stage_label),
      summary, elapsed, chevron);
    const panel = h("div", { class: "hidden border-t border-zinc-100 bg-zinc-50/60 px-6 py-5" });
    const li = h("li", { dataset: { stage: stage.stage_key } }, head, panel);
    const row = { li, head, panel, dot, word, elapsed, summary, chevron, stage: { ...stage }, rendered: false };
    head.addEventListener("click", () => toggle(row));
    rows.set(stage.stage_key, row);
    list.appendChild(li);
    return row;
  }

  function toggle(row, open) {
    if (!row.stage.detail || ["pending", "running"].includes(row.stage.status)) return;
    const willOpen = open ?? row.panel.classList.contains("hidden");
    if (willOpen && !row.rendered) {
      clear(row.panel).appendChild(renderPanel(row.stage, { viewer, record }));
      row.rendered = true;
    }
    row.panel.classList.toggle("hidden", !willOpen);
    row.head.setAttribute("aria-expanded", String(willOpen));
    row.chevron.classList.toggle("rotate-90", willOpen);
  }

  function paint(row) {
    const s = row.stage;
    const st = STAGE_STATUS[s.status] || STAGE_STATUS.pending;
    row.dot.className = `h-2.5 w-2.5 shrink-0 rounded-full transition-colors duration-150 ${st.dot}`;
    row.word.className = `w-16 text-xs font-medium ${st.text}`;
    row.word.textContent = st.word;
    row.summary.textContent = s.status === "running" ? "Running…" : (s.summary || "");
    row.summary.title = s.summary || "";
    row.summary.classList.toggle("text-zinc-400", ["pending", "skipped"].includes(s.status));
    if (s.status !== "running") row.elapsed.textContent = ms(s.duration_ms);
    const expandable = Boolean(s.detail && Object.keys(s.detail).length) && !["pending", "running"].includes(s.status);
    row.head.disabled = !expandable;
    row.chevron.classList.toggle("invisible", !expandable);
  }

  function update(stage) {
    const row = ensureRow(stage);
    row.stage = { ...row.stage, ...stage };
    row.rendered = false;
    if (!row.panel.classList.contains("hidden")) toggle(row, true);
    paint(row);
    if (stage.stage_key === "ingest" && stage.status !== "running") onIngest(row.stage);
    if (stage.stage_key === "ocr" && stage.status !== "running") onOcr(row.stage);
  }

  function onIngest(stage) {
    if (["ok", "warn"].includes(stage.status) && stage.detail && stage.detail.pages) {
      viewer.setDocument({ runId, pageCount: stage.detail.pages.length });
    } else if (stage.status === "failed") {
      viewer.setUnavailable(`No preview: ${stage.summary}`);
    }
  }

  function onOcr(stage) {
    const pages = (stage.detail && stage.detail.pages) || [];
    const processed = pages.some((p) => p.processed);
    if (processed) {
      const ingest = rows.get("ingest");
      viewer.setDocument({ runId, pageCount: ingest.stage.detail.pages.length, hasProcessed: true, boxesOnProcessed: true });
    }
  }

  // Running rows show a live clock from the server's started_at.
  function tick() {
    for (const row of rows.values()) {
      if (row.stage.status === "running" && row.stage.started_at) {
        row.elapsed.textContent = ms(Math.max(0, Date.now() - Date.parse(row.stage.started_at)));
      }
    }
  }

  // ---------------------------------------------------------- field <-> box link

  function revealField(path) {
    const row = rows.get("extract");
    if (!row) return;
    toggle(row, true);
    const tr = row.panel.querySelector(`[data-field-path="${CSS.escape(path)}"]`);
    if (!tr) return;
    tr.scrollIntoView({ behavior: "smooth", block: "center" });
    tr.classList.remove("field-flash");
    void tr.offsetWidth;            // restart the highlight animation
    tr.classList.add("field-flash");
  }

  // ---------------------------------------------------------- lifecycle

  async function finish(payload) {
    if (es) { es.close(); es = null; }
    clearInterval(ticker);
    conn.textContent = "";
    clear(cardSlot).appendChild(renderDecisionCard(payload.card, payload, { runId, onChanged: refresh }));
    const verdict = payload.card ? payload.card.verdict : "No decision";
    document.title = `${verdict} · ${filename} — Invoice Decisioning`;
    await refresh();
  }

  async function refresh() {
    try {
      record = await api.run(runId);
    } catch (err) {
      showError(`Could not load run ${runId}: ${err.message}`);
      return;
    }
    viewer.setFields(record.fields);
    if (record.card) {
      clear(cardSlot).appendChild(renderDecisionCard(record.card, { status: record.run.status, error: record.run.error },
                                                      { runId, onChanged: refresh }));
    }
    for (const row of rows.values()) row.rendered = false;
    onRecord && onRecord(record, extras);
  }

  async function start() {
    try {
      record = await api.run(runId);
    } catch (err) {
      showError(`Could not open run ${runId}: ${err.message}`);
      return;
    }
    filename = record.run.source_filename || runId;
    document.title = `Running · ${filename} — Invoice Decisioning`;
    for (const s of record.stages) update(s);
    ticker = setInterval(tick, 100);

    es = new EventSource(api.eventsUrl(runId));
    const onStage = (e) => { conn.textContent = ""; update(JSON.parse(e.data)); };
    es.addEventListener("stage_started", onStage);
    es.addEventListener("stage_finished", onStage);
    es.addEventListener("run_finished", (e) => finish(JSON.parse(e.data)));
    es.addEventListener("run_failed", (e) => finish(JSON.parse(e.data)));
    es.onerror = () => {
      if (!es) return;
      if (es.readyState === EventSource.CLOSED) {
        showError("The live stream closed unexpectedly. Refresh to reload this run from the database.");
      } else {
        conn.textContent = "Reconnecting…";    // EventSource retries; the server replays history on reconnect
      }
    };
  }

  function destroy() {
    if (es) { es.close(); es = null; }
    clearInterval(ticker);
  }

  return { el, start, destroy, viewer };
}

// ================================================================ decision card

export function renderDecisionCard(card, payload, { runId, onChanged, actions } = {}) {
  if (!card) {
    return h("section", { class: "slide-in rounded-xl border border-rose-200 bg-white p-6" },
      h("div", { class: "flex items-center gap-2 text-lg font-semibold text-rose-700" }, icon("alert"), "No decision"),
      h("p", { class: "mt-2 text-sm text-zinc-700" }, payload && payload.error ? payload.error : "The run did not reach a decision."));
  }
  const v = VERDICT[card.verdict];
  const reasons = card.reason_codes.length
    ? h("div", { class: "mt-4 flex flex-wrap gap-2" },
        card.reason_codes.map((c) => codeChip(c, ["BLOCK", "REJECT"].includes(card.verdict) ? "rose" : "amber")))
    : null;
  const figures = h("dl", { class: "mt-6 grid grid-cols-5 gap-px overflow-hidden rounded-lg border border-zinc-200 bg-zinc-200" },
    card.figures.map((f) => h("div", { class: "bg-white px-3 py-3" },
      h("dt", { class: "text-xs text-zinc-500" }, f.label),
      h("dd", { class: `mono mt-1 truncate text-sm ${f.tone === "fail" ? "text-rose-700" : "text-zinc-900"}`, title: f.value }, f.value))));

  return h("section", { class: `slide-in rounded-xl border bg-white p-6 shadow-sm ${v.ring}`, "aria-live": "polite" },
    h("div", { class: "flex items-start justify-between gap-4" },
      h("div", {},
        h("div", { class: `inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-2xl font-semibold tracking-tight ${v.badge}` },
          icon(v.glyph, "scale-125"), card.verdict_words),
        h("p", { class: "mono mt-2 text-xs text-zinc-500" }, `${card.verdict} · branch ${card.branch}`)),
      card.payment_date
        ? h("div", { class: "text-right" }, h("p", { class: "text-xs text-zinc-500" }, "Pay on"),
            h("p", { class: "mono text-lg text-zinc-900" }, card.payment_date))
        : null),
    reasons,
    h("p", { class: "mt-4 text-[15px] leading-relaxed text-zinc-700" }, card.explanation),
    h("div", { class: "mt-4 flex items-start gap-3 rounded-lg bg-zinc-50 px-4 py-3" },
      icon("arrowRight", "mt-0.5 text-zinc-400"),
      h("div", { class: "text-sm" },
        h("p", { class: "font-medium text-zinc-900" }, card.next_action),
        h("p", { class: "mt-0.5 text-zinc-500" }, `Owner: ${card.owner}`))),
    figures,
    actions || null);
}

// ================================================================ stage panels

function renderPanel(stage, ctx) {
  const d = stage.detail || {};
  const fn = PANELS[stage.stage_key];
  const body = fn ? fn(d, ctx, stage) : kv(d);
  return h("div", {}, body, rawToggle(d));
}

function rawToggle(d) {
  const pre = h("pre", { class: "mono mt-3 hidden max-h-96 overflow-auto rounded-lg border border-zinc-200 bg-white p-4 text-xs text-zinc-700" },
    JSON.stringify(d, null, 2));
  const btn = h("button", { type: "button", class: "focus-ring mt-4 text-xs text-zinc-500 underline-offset-2 hover:underline",
                            onclick: () => { pre.classList.toggle("hidden"); btn.textContent = pre.classList.contains("hidden") ? "View raw" : "Hide raw"; } },
    "View raw");
  return h("div", {}, btn, pre);
}

const words = (k) => k.replace(/_minor$/, "").replace(/_/g, " ").replace(/(po|grn|ifsc|psm|ocr|id)/gi, (w) => w.toUpperCase())
  .replace(/^./, (c) => c.toUpperCase());

function scalar(v, key = "") {
  if (v === null || v === undefined || v === "") return h("span", { class: "text-zinc-400" }, "—");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number" && key.endsWith("_minor")) return h("span", { class: "mono" }, money(v));
  if (typeof v === "number") return h("span", { class: "mono" }, String(v));
  return String(v);
}

export function kv(obj, depth = 0) {
  if (obj === null || typeof obj !== "object") return scalar(obj);
  const entries = Object.entries(obj).filter(([k]) => k !== "traceback");
  if (!entries.length) return h("p", { class: "text-sm text-zinc-500" }, "Nothing recorded for this stage.");
  const tb = obj.traceback ? h("pre", { class: "mono mt-3 overflow-auto rounded-lg bg-rose-50 p-3 text-xs text-rose-800" }, obj.traceback) : null;
  return h("div", {},
    h("dl", { class: "grid grid-cols-[minmax(0,11rem)_1fr] gap-x-4 gap-y-2 text-sm" },
      entries.map(([k, v]) => [
        h("dt", { class: "text-zinc-500" }, words(k)),
        h("dd", { class: "min-w-0 break-words text-zinc-800" }, value(v, k, depth)),
      ])),
    tb);
}

function value(v, k, depth) {
  if (Array.isArray(v)) {
    if (!v.length) return h("span", { class: "text-zinc-400" }, "none");
    if (v.every((x) => x === null || typeof x !== "object")) return v.map((x) => String(x)).join(", ");
    if (depth < 2) return table(v);
    return h("span", { class: "mono text-xs" }, JSON.stringify(v));
  }
  if (v && typeof v === "object") return depth < 2 ? kv(v, depth + 1) : h("span", { class: "mono text-xs" }, JSON.stringify(v));
  return scalar(v, k);
}

export function table(rows, columns) {
  const cols = columns || [...new Set(rows.flatMap((r) => Object.keys(r || {})))].map((k) => ({ key: k, label: words(k) }));
  return h("div", { class: "overflow-x-auto rounded-lg border border-zinc-200 bg-white" },
    h("table", { class: "w-full text-left text-xs" },
      h("thead", { class: "bg-zinc-50 text-zinc-500" },
        h("tr", {}, cols.map((c) => h("th", { class: `px-3 py-2 font-medium ${c.right ? "text-right" : ""}` }, c.label)))),
      h("tbody", { class: "divide-y divide-zinc-100" },
        rows.map((r) => h("tr", { class: (r && r._rowClass) || "" },
          cols.map((c) => h("td", { class: `px-3 py-2 align-top ${c.right ? "text-right" : ""} ${c.mono ? "mono" : ""}` },
            c.render ? c.render(r) : (r && typeof r[c.key] === "object" && r[c.key] !== null
              ? JSON.stringify(r[c.key]) : scalar(r ? r[c.key] : null, c.key)))))))));
}

function section(title, ...children) {
  return h("div", { class: "mt-5 first:mt-0" },
    h("h3", { class: "mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500" }, title), children);
}

const PANELS = {
  ocr(d) {
    if (!d.pages) return kv(d);
    return h("div", {},
      section("Legibility by page", h("div", { class: "space-y-2" }, d.pages.map((p) =>
        h("div", { class: "flex items-center gap-3 text-sm" },
          h("span", { class: "w-14 text-zinc-500" }, `Page ${p.page}`),
          h("span", { class: "relative h-2 flex-1 overflow-hidden rounded-full bg-zinc-200" },
            h("span", { class: `absolute inset-y-0 left-0 ${p.legibility >= d.legibility_min ? "bg-emerald-600" : "bg-amber-500"}`,
                        style: { width: `${Math.round((p.legibility || 0) * 100)}%` } })),
          h("span", { class: "mono w-12 text-right" }, p.legibility === null || p.legibility === undefined ? "—" : p.legibility.toFixed(2)),
          h("span", { class: "mono w-28 text-xs text-zinc-500" }, p.psm ? `PSM ${p.psm} won` : ""))))),
      d.steps ? section("Preprocessing", table(d.steps)) : null,
      section("Details", kv(Object.fromEntries(Object.entries(d).filter(([k]) => !["pages", "steps"].includes(k))))));
  },

  extract(d, ctx) {
    const usage = d.usage || {};
    const meta = [d.source === "cache" ? "cache hit" : d.source, d.model, d.prompt_version && `prompt ${d.prompt_version}`,
                  d.source !== "cache" && usage.input_tokens != null && `${usage.input_tokens.toLocaleString()} in / ${usage.output_tokens.toLocaleString()} out tokens`,
                  d.attempts && `${d.attempts} attempt${d.attempts > 1 ? "s" : ""}`].filter(Boolean).join(" · ");
    const fields = d.fields || [];
    const tbody = h("tbody", { class: "divide-y divide-zinc-100" }, fields.map((f) =>
      h("tr", {
        class: "field-row cursor-default transition-colors", dataset: { fieldPath: f.field },
        onmouseenter: () => ctx.viewer.highlight(f.field), onmouseleave: () => ctx.viewer.unhighlight(),
      },
        h("td", { class: "mono whitespace-nowrap px-3 py-2 align-top text-zinc-600" }, f.field),
        h("td", { class: "px-3 py-2 align-top text-zinc-900" },
          f.value === null ? h("span", { class: "text-rose-700" }, "not read") : f.value),
        h("td", { class: "px-3 py-2 align-top" }, confidenceBar(f.confidence)),
        h("td", { class: "max-w-[14rem] truncate px-3 py-2 align-top italic text-zinc-500", title: f.evidence || "" }, f.evidence || "—"))));
    return h("div", {},
      h("p", { class: "mono text-xs text-zinc-500" }, meta),
      d.unreadable_regions && d.unreadable_regions.length
        ? section("Unreadable regions", table(d.unreadable_regions)) : null,
      d.notes && d.notes.length ? section("Model notes", h("ul", { class: "list-disc space-y-1 pl-5 text-sm text-zinc-700" }, d.notes.map((n) => h("li", {}, typeof n === "string" ? n : JSON.stringify(n))))) : null,
      section(`Fields (${fields.filter((f) => f.value !== null).length} of ${fields.length} read) — hover a row to find it on the page`,
        h("div", { class: "max-h-[28rem] overflow-auto rounded-lg border border-zinc-200 bg-white" },
          h("table", { class: "w-full text-left text-xs" },
            h("thead", { class: "sticky top-0 bg-zinc-50 text-zinc-500" },
              h("tr", {}, ["Field", "Value", "Confidence", "Evidence"].map((c) => h("th", { class: "px-3 py-2 font-medium" }, c)))),
            tbody))));
  },

  normalise(inv) {
    const cur = inv.currency || "INR";
    const m = (x) => money(x, cur);
    return h("div", {},
      kv({
        invoice_number: inv.invoice_number, invoice_date: inv.invoice_date,
        date_alternatives: inv.invoice_date_alternatives && inv.invoice_date_alternatives.length > 1 ? inv.invoice_date_alternatives : null,
        due_date: inv.due_date, currency: `${cur} (from ${inv.currency_source || "—"})`,
        supplier: inv.supplier_name_canonical, supplier_gstin: inv.supplier_tax_id, supplier_email: inv.supplier_email,
        po_references: (inv.po_refs || []).map((r) => `${r.value} (${r.explicit ? "explicit" : "implied"}, ${r.source})`),
        bank_account: inv.bank_account_last4 ? `…${inv.bank_account_last4} / ${inv.bank_ifsc || "—"}` : null,
        supplied_by_clerk: inv.human_supplied && inv.human_supplied.length ? inv.human_supplied : null,
      }),
      section("Lines", table(inv.lines || [], [
        { key: "line_no", label: "#", mono: true }, { key: "sku", label: "SKU", mono: true },
        { key: "description", label: "Description" }, { key: "qty", label: "Qty", right: true, mono: true },
        { key: "unit_price_minor", label: "Rate", right: true, mono: true, render: (r) => m(r.unit_price_minor) },
        { key: "tax_rate_pct", label: "GST %", right: true, mono: true },
        { key: "line_total_minor", label: "Amount", right: true, mono: true, render: (r) => m(r.line_total_minor) },
      ])),
      section("Totals", h("dl", { class: "ml-auto grid max-w-xs grid-cols-2 gap-y-1 text-sm" },
        [["Subtotal", inv.subtotal_minor], ...(inv.tax_breakdown || []).map((t) => [`${t.label}${t.rate_pct ? ` @ ${t.rate_pct}%` : ""}`, t.amount_minor]),
         ["Tax total", inv.tax_total_minor], ["Discount", inv.discount_minor], ["Shipping", inv.shipping_minor],
         ["Grand total", inv.grand_total_minor]]
          .filter(([, v]) => v !== null && v !== undefined)
          .map(([k, v]) => [h("dt", { class: "text-zinc-500" }, k), h("dd", { class: "mono text-right text-zinc-900" }, m(v))]))),
      inv.warnings && inv.warnings.length ? section("Warnings", h("ul", { class: "list-disc space-y-1 pl-5 text-sm text-amber-800" }, inv.warnings.map((w) => h("li", {}, w)))) : null);
  },

  selfcheck(d) {
    return h("div", {},
      table((d.checks || []).map((c) => ({ ...c, _rowClass: c.ok ? "" : "bg-rose-50/60" })), [
        { key: "check", label: "Check" },
        { key: "stated_minor", label: "Stated", right: true, mono: true, render: (c) => money(c.stated_minor) },
        { key: "expected_minor", label: "Computed", right: true, mono: true, render: (c) => money(c.expected_minor) },
        { key: "delta_minor", label: "Difference", right: true, mono: true, render: (c) => money(c.delta_minor) },
        { key: "ok", label: "Result", render: (c) => ruleChip(c.ok ? "pass" : "fail") },
        { key: "suspect", label: "Likely cause", render: (c) => c.suspect || "" },
      ]),
      d.derived && Object.keys(d.derived).length ? section("Derived (not printed)", kv(d.derived)) : null,
      h("p", { class: "mt-3 text-xs text-zinc-500" }, `Rounding tolerance ${money(d.tolerance_minor)}.`));
  },

  match(d) {
    const v = d.vendor || {};
    const po = d.po || {};
    const c = d.consumption;
    const TIER = { T1: "explicit reference, this vendor", T2: "quoted PO belongs to another vendor",
                   T3: "implied match by score" };
    const winner = po.po_number;
    return h("div", {},
      section("Vendor", kv({ vendor: v.vendor_id ? `${v.vendor_id} · ${v.legal_name}` : "not found in the vendor master",
                             status: v.status, matched_by: v.method, score: v.score, ambiguous: v.ambiguous })),
      section("Purchase order", kv({ tier: po.tier ? `${po.tier} · ${TIER[po.tier]}` : "no match", po: winner || po.quoted_po_number,
                                     reason: po.reason })),
      po.candidates && po.candidates.length ? section("Candidate ranking", table(
        po.candidates.map((x) => ({ ...x, _rowClass: x.po_number === winner ? "bg-indigo-50/70" : "" })), [
          { key: "po_number", label: "PO", mono: true, render: (x) => x.po_number === winner ? `${x.po_number}  ← matched` : x.po_number },
          { key: "score", label: "Score", right: true, mono: true, render: (x) => x.score.toFixed(2) },
          { key: "amount", label: "Amount", right: true, mono: true, render: (x) => x.amount.toFixed(2) },
          { key: "date", label: "Date", right: true, mono: true, render: (x) => x.date.toFixed(2) },
          { key: "lines", label: "Lines", right: true, mono: true, render: (x) => x.lines.toFixed(2) },
          { key: "remaining_minor", label: "Remaining", right: true, mono: true, render: (x) => money(x.remaining_minor) },
          { key: "status", label: "Status" },
        ])) : null,
      c ? section("Consumption", kv({ po_total_minor: c.po_total_minor, consumed_minor: c.consumed_minor,
                                      remaining_minor: c.remaining_minor, this_invoice_minor: c.this_invoice_minor,
                                      prior_invoices: c.prior_invoices })) : null,
      d.grn ? section("Goods receipts", h("div", {},
        h("p", { class: "mb-2 text-sm text-zinc-700" }, d.grn.grn_numbers.length ? d.grn.grn_numbers.join(", ") : "No GRN recorded"),
        d.grn.lines && d.grn.lines.length ? table(d.grn.lines) : null)) : null,
      d.duplicates ? section("Duplicates", kv({ exact: d.duplicates.exact, near: d.duplicates.near })) : null);
  },

  rules(d) {
    const counts = d.counts || {};
    return h("div", {},
      h("p", { class: "mb-3 text-sm text-zinc-600" },
        Object.entries(counts).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(" · ")),
      ruleTable(d.rules || []));
  },

  decide(d) {
    const fired = (d.branch || "").split(" ·")[0];
    return h("div", {},
      h("ol", { class: "space-y-2" }, (d.matrix || []).map((b) => {
        const on = b.branch === fired;
        return h("li", { class: `flex items-start gap-3 rounded-lg border px-4 py-3 text-sm ${on ? "border-indigo-300 bg-indigo-50" : "border-zinc-200 bg-white text-zinc-500"}` },
          h("span", { class: `mono w-5 ${on ? "text-indigo-700" : ""}` }, b.branch),
          h("span", { class: "flex-1" }, `If ${b.when} → `, h("span", { class: "mono" }, b.then)),
          on ? h("span", { class: "text-xs font-medium text-indigo-700" }, "fired") : null);
      })),
      h("p", { class: "mt-3 text-sm text-zinc-700" }, d.branch),
      d.confidence != null ? h("p", { class: "mt-1 text-xs text-zinc-500" }, `Run confidence ${d.confidence.toFixed(2)}`) : null);
  },
};

export function ruleTable(rules) {
  return table(rules.map((r) => ({ ...r, _rowClass: r.status === "fail" ? "bg-rose-50/60" : r.status === "warn" ? "bg-amber-50/50" : "" })), [
    { key: "code", label: "Code", mono: true, render: (r) => r.code || r.rule_code },
    { key: "name", label: "Rule", render: (r) => r.name || r.rule_name },
    { key: "severity", label: "Severity", render: (r) => h("span", { class: `text-xs ${r.severity === "hard" ? "font-medium text-zinc-900" : "text-zinc-500"}` }, r.severity) },
    { key: "status", label: "Status", render: (r) => ruleChip(r.status) },
    { key: "message", label: "Message", render: (r) => h("span", { class: "text-zinc-700" }, r.message) },
  ]);
}
