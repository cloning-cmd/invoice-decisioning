// The decision card, the human actions on it, and the exception panels that
// explain a specific verdict (bank change, unreadable field, PO consumption,
// suspected duplicate). Shared by the live run view and the run-detail screen.

import { api } from "./api.js";
import { VERDICT, clear, codeChip, getRole, h, icon, modal, money, showError, when } from "./ui.js";

// ================================================================ card

export function renderDecisionCard(card, payload, ctx = {}) {
  if (!card) {
    return h("section", { class: "slide-in rounded-xl border border-rose-200 bg-white p-6" },
      h("div", { class: "flex items-center gap-2 text-lg font-semibold text-rose-700" }, icon("alert"), "No decision"),
      h("p", { class: "mt-2 text-sm text-zinc-700" }, payload && payload.error ? payload.error : "The run did not reach a decision."));
  }
  const v = VERDICT[card.verdict];
  const hard = ["BLOCK", "REJECT"].includes(card.verdict);
  const figures = h("dl", { class: "mt-6 grid grid-cols-3 gap-px xl:grid-cols-5 overflow-hidden rounded-lg border border-zinc-200 bg-zinc-200" },
    card.figures.map((f) => h("div", { class: "bg-white px-3 py-3" },
      h("dt", { class: "text-xs text-zinc-500" }, f.label),
      h("dd", { class: `mono mt-1 break-words text-sm ${f.tone === "fail" ? "text-rose-700" : "text-zinc-900"}` }, f.value))));

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
    card.reason_codes.length
      ? h("div", { class: "mt-4 flex flex-wrap gap-2" }, card.reason_codes.map((c) => codeChip(c, hard ? "rose" : "amber")))
      : null,
    h("p", { class: "mt-4 text-[15px] leading-relaxed text-zinc-700" }, card.explanation),
    h("div", { class: `mt-4 flex items-start gap-3 rounded-lg px-4 py-3 ${card.verdict === "BLOCK" ? "bg-rose-50" : "bg-zinc-50"}` },
      icon("arrowRight", `mt-0.5 ${card.verdict === "BLOCK" ? "text-rose-600" : "text-zinc-400"}`),
      h("div", { class: "text-sm" },
        h("p", { class: `font-medium ${card.verdict === "BLOCK" ? "text-rose-900" : "text-zinc-900"}` }, card.next_action),
        h("p", { class: "mt-0.5 text-zinc-500" }, `Owner: ${card.owner}`))),
    figures,
    card.resolution ? resolutionStrip(card.resolution) : null,
    ctx.record ? actionRow(card, ctx) : null);
}

function resolutionStrip(r) {
  const ok = r.action === "override_approve";
  return h("div", { class: `mt-4 flex items-start gap-3 rounded-lg border px-4 py-3 text-sm ${ok ? "border-emerald-200 bg-emerald-50" : "border-rose-200 bg-rose-50"}` },
    icon(ok ? "check" : "x", `mt-0.5 ${ok ? "text-emerald-700" : "text-rose-700"}`),
    h("div", {},
      h("p", { class: "font-medium text-zinc-900" }, `${r.words} by ${r.actor_name}`,
        h("span", { class: "font-normal text-zinc-500" }, ` · ${when(r.created_at)}`)),
      h("p", { class: "mt-0.5 text-zinc-700" }, `“${r.note}”`)));
}

// ================================================================ actions

const ACTION_COPY = {
  override_approve: { title: "Approve for payment", confirm: "Approve", tone: "emerald",
                      help: "Overrides the controls and releases this invoice for payment. The system decision stays on record." },
  override_reject: { title: "Reject", confirm: "Reject", tone: "rose",
                     help: "Rejects the invoice. The system decision stays on record next to yours." },
  note_added: { title: "Add a note", confirm: "Add note", tone: "zinc", help: "Recorded in the audit trail." },
  field_supplied: { title: "Supply a missing field", confirm: "Supply and re-decide", tone: "indigo",
                    help: "Your value replaces the unreadable reading and the run is decided again from Normalise onward. The model is not called." },
};

function actionRow(card, ctx) {
  const { record } = ctx;
  const role = getRole();
  const canOverride = card.override_roles.includes(role);
  const resolved = Boolean(card.resolution);
  const suppliable = record.suppliable_fields || [];
  const btn = (label, action, style, extra = {}) => h("button", {
    type: "button", class: `focus-ring inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${style}`,
    onclick: () => openAction(action, ctx, extra),
  }, label);
  const primary = "bg-indigo-600 text-white hover:bg-indigo-700";
  const secondary = "border border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-50";
  const danger = "border border-rose-200 bg-white text-rose-700 hover:bg-rose-50";

  const buttons = [];
  const locked = card.verdict === "BLOCK" && !canOverride && !resolved;
  if (!resolved) {
    if (suppliable.length && ["REJECT", "HOLD_FOR_REVIEW"].includes(card.verdict)) {
      buttons.push(btn("Supply missing field", "field_supplied", primary));
    }
    if (canOverride) {
      if (card.verdict !== "AUTO_APPROVE") {
        buttons.push(btn(card.verdict === "APPROVE_PENDING_SIGNOFF" ? "Sign off" : "Approve anyway", "override_approve",
                         card.verdict === "APPROVE_PENDING_SIGNOFF" ? primary : secondary));
      }
      if (card.verdict !== "REJECT") buttons.push(btn("Reject", "override_reject", danger));
    }
  }
  buttons.push(btn("Add note", "note_added", secondary));

  return h("div", { class: "mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-zinc-100 pt-5" },
    locked
      ? h("p", { class: "flex items-center gap-2 text-sm text-rose-800" }, icon("lock"),
          `Locked. Only the ${record.roles.ap_manager.split(", ")[1]} can release or reject a blocked invoice.`)
      : h("p", { class: "text-xs text-zinc-500" }, `Acting as ${record.roles[role]}`),
    h("div", { class: "flex flex-wrap gap-2" }, buttons));
}

export function openAction(action, ctx, { field, notePlaceholder } = {}) {
  const { record, runId, onChanged } = ctx;
  const copy = ACTION_COPY[action];
  const suppliable = record.suppliable_fields || [];
  const fieldSel = action === "field_supplied"
    ? h("select", { class: "mono focus-ring mt-1 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm", name: "field" },
        suppliable.map((f) => h("option", { value: f, selected: f === field }, f)))
    : null;
  const valueIn = action === "field_supplied"
    ? h("input", { class: "mono focus-ring mt-1 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm", name: "value",
                   required: true, autocomplete: "off", placeholder: "Exactly as printed on the document" })
    : null;
  const noteIn = h("textarea", { class: "focus-ring mt-1 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm", rows: "3",
                                 required: true, name: "note", placeholder: notePlaceholder || "Why? This goes in the audit trail." });
  const body = h("div", { class: "space-y-4" },
    h("p", { class: "text-sm text-zinc-600" }, copy.help),
    fieldSel ? h("label", { class: "block text-sm font-medium text-zinc-800" }, "Field", fieldSel) : null,
    valueIn ? h("label", { class: "block text-sm font-medium text-zinc-800" }, "Value", valueIn) : null,
    h("label", { class: "block text-sm font-medium text-zinc-800" }, "Note", noteIn),
    h("p", { class: "text-xs text-zinc-500" }, `Recorded as ${record.roles[getRole()]}.`));

  modal({
    title: copy.title, body, confirmLabel: copy.confirm, tone: copy.tone,
    async onConfirm() {
      const payload = action === "field_supplied" ? { field: fieldSel.value, value: valueIn.value.trim() } : null;
      if (action === "field_supplied" && !payload.value) throw new Error("Type the value as it appears on the document.");
      if (!noteIn.value.trim()) throw new Error("A note is required.");
      await api.action(runId, { action, actor: getRole(), note: noteIn.value.trim(), payload });
      await onChanged();
    },
  });
}

// ================================================================ exception panels

export function renderInsights(record, ctx) {
  const panels = [];
  const r12 = record.rules.find((r) => r.rule_code === "R12_BANK_DETAILS_UNCHANGED");
  if (r12 && (r12.status === "fail" || r12.status === "warn")) panels.push(bankPanel(r12, record));
  const regions = (record.unreadable_regions || []).filter((r) => Array.isArray(r.box_2d));
  if (regions.length) panels.push(regionPanel(regions, record, ctx));
  const dups = record.duplicates || {};
  if ((dups.near && dups.near.length) || (dups.exact && dups.exact.length)) panels.push(duplicatePanel(record, ctx));
  if (record.consumption) panels.push(consumptionPanel(record));
  return panels;
}

function panel(title, glyph, tone, ...children) {
  const tones = {
    rose: "border-rose-300 bg-rose-50/60", amber: "border-amber-300 bg-amber-50/40", zinc: "border-zinc-200 bg-white",
  };
  const titleTone = { rose: "text-rose-800", amber: "text-amber-900", zinc: "text-zinc-900" };
  return h("section", { class: `slide-in rounded-xl border p-6 ${tones[tone]}` },
    h("h2", { class: `mb-4 flex items-center gap-2 text-sm font-semibold ${titleTone[tone]}` }, icon(glyph), title),
    children);
}

// ---- Edge 4: bank details changed

function bankPanel(r12, record) {
  const e = r12.evidence || {};
  const rows = [
    ["Account", e.master_last4 ? `…${e.master_last4}` : "—", e.invoice_last4 ? `…${e.invoice_last4}` : "not printed",
     e.invoice_last4 && e.master_last4 && e.invoice_last4 !== e.master_last4],
    ["IFSC / SWIFT", e.master_ifsc || "—", e.invoice_ifsc || "not printed",
     e.invoice_ifsc && e.master_ifsc && e.invoice_ifsc !== e.master_ifsc],
    ["Email domain", e.master_email_domain || "—", e.invoice_email_domain || "not printed", Boolean(e.email_domain_mismatch)],
  ];
  const failed = r12.status === "fail";
  return panel(failed ? "Bank details changed: payment locked" : "Sender domain differs from the vendor master",
    failed ? "lock" : "alert", failed ? "rose" : "amber",
    h("div", { class: "overflow-hidden rounded-lg border border-rose-200 bg-white" },
      h("table", { class: "w-full text-left text-sm" },
        h("thead", { class: "bg-zinc-50 text-xs text-zinc-500" },
          h("tr", {}, ["", "Vendor master", "This invoice", ""].map((c) => h("th", { class: "px-4 py-2 font-medium" }, c)))),
        h("tbody", { class: "divide-y divide-zinc-100" }, rows.map(([label, master, inv, diff]) =>
          h("tr", { class: diff ? "bg-rose-50/70" : "" },
            h("td", { class: "px-4 py-2.5 text-zinc-500" }, label),
            h("td", { class: "mono px-4 py-2.5 text-zinc-900" }, master),
            h("td", { class: `mono px-4 py-2.5 ${diff ? "font-semibold text-rose-700" : "text-zinc-900"}` }, inv),
            h("td", { class: "px-4 py-2.5 text-right text-xs" },
              diff ? h("span", { class: "inline-flex items-center gap-1 text-rose-700" }, icon("x"), "changed")
                   : h("span", { class: "inline-flex items-center gap-1 text-emerald-700" }, icon("check"), "matches"))))))),
    failed ? h("p", { class: "mt-4 text-sm font-medium text-rose-900" },
      "Verify by phone on the number in the vendor master. Do not reply to the invoice email: the sender may not be the vendor.") : null);
}

// ---- Edge 1: the region the model could not read

function regionPanel(regions, record, ctx) {
  const items = regions.map((reg) => {
    const canvas = h("canvas", { class: "block max-w-full rounded-lg border border-zinc-200 bg-zinc-100", role: "img",
                                  "aria-label": `Crop of page ${reg.page}: ${reg.description}` });
    const img = new Image();
    img.onload = () => {
      // box_2d is [ymin, xmin, ymax, xmax] on a 0-1000 scale; pad it so the clerk sees context.
      const [y0, x0, y1, x1] = reg.box_2d;
      const pad = 40;
      const sx = Math.max(0, (x0 - pad) / 1000) * img.naturalWidth;
      const sy = Math.max(0, (y0 - pad) / 1000) * img.naturalHeight;
      const sw = Math.min(1, (x1 + pad) / 1000) * img.naturalWidth - sx;
      const sh = Math.min(1, (y1 + pad) / 1000) * img.naturalHeight - sy;
      const scale = Math.min(560 / sw, 2.5);
      canvas.width = Math.round(sw * scale);
      canvas.height = Math.round(sh * scale);
      canvas.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
    };
    img.onerror = () => showError(`Could not load page ${reg.page} to crop the unreadable region.`);
    img.src = api.pageUrl(ctx.runId, reg.page, "original");
    return h("figure", { class: "space-y-2" }, canvas,
      h("figcaption", { class: "text-sm text-zinc-600" }, `Page ${reg.page}: ${reg.description}`));
  });
  const supplied = Object.entries(record.supplied_fields || {});
  const suppliable = record.suppliable_fields || [];
  const field = suppliable.find((f) => f === "invoice_number") || suppliable[0];
  return panel("The model could not read this", "image", "amber",
    h("div", { class: "space-y-4" }, items),
    supplied.length
      ? h("p", { class: "mt-4 flex items-center gap-2 text-sm text-emerald-800" }, icon("check"),
          `Supplied by a clerk: ${supplied.map(([f, v]) => `${f} = ${v}`).join(", ")}`)
      : field && ["REJECT", "HOLD_FOR_REVIEW"].includes(record.run.decision)
        ? h("div", { class: "mt-4 flex items-center justify-between gap-3" },
            h("p", { class: "text-sm text-zinc-700" }, "Read it from the source document and type it in; the run is re-decided without calling the model."),
            h("button", { type: "button", class: "focus-ring shrink-0 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700",
                          onclick: () => openAction("field_supplied", { ...ctx, record }, { field, notePlaceholder: "Where did you read it? e.g. the paper copy in the inward register" }) },
              `Supply ${field}`))
        : null);
}

// ---- Edge 2: cumulative PO consumption

function consumptionPanel(record) {
  const c = record.consumption;
  const cur = c.currency || "INR";
  const priors = c.prior_invoices || [];
  const thisAmt = c.this_invoice_minor || 0;
  const after = c.consumed_minor + thisAmt;
  const ceiling = c.po_total_minor;
  const scaleMax = Math.max(ceiling + c.tolerance_minor, after) * 1.04;
  const pct = (x) => `${(x / scaleMax) * 100}%`;
  const over = Math.max(0, after - ceiling);
  const segs = [];
  let x = 0;
  priors.forEach((p, i) => {
    segs.push(h("div", { class: `absolute inset-y-0 border-r border-white ${i % 2 ? "bg-zinc-500" : "bg-zinc-400"}`,
                         style: { left: pct(x), width: pct(p.total_minor) }, title: `${p.invoice_number} ${money(p.total_minor, cur)} (${p.status})` }));
    x += p.total_minor;
  });
  const within = Math.max(0, Math.min(thisAmt, ceiling - x));
  if (within > 0) segs.push(h("div", { class: "absolute inset-y-0 bg-indigo-500", style: { left: pct(x), width: pct(within) },
                                         title: `This invoice ${money(thisAmt, cur)}` }));
  if (over > 0) segs.push(h("div", { class: "stripes absolute inset-y-0", style: { left: pct(Math.max(x, ceiling)), width: pct(Math.min(over, thisAmt)) },
                                       title: `${money(over, cur)} over the PO` }));
  const legend = [
    ...priors.map((p, i) => [i % 2 ? "bg-zinc-500" : "bg-zinc-400", `${p.invoice_number} (${p.status})`, money(p.total_minor, cur)]),
    ["bg-indigo-500", "This invoice", money(thisAmt, cur)],
    ...(over > 0 ? [["stripes", "Over the PO", money(over, cur)]] : []),
    ["border-2 border-dashed border-amber-500", "Tolerance beyond the PO", `+${money(c.tolerance_minor, cur)}`],
  ];
  const overTol = after > ceiling + c.tolerance_minor;
  return panel(`${c.po_number} consumption`, "file", overTol ? "rose" : "zinc",
    h("div", { class: "relative h-7 overflow-hidden rounded-md bg-zinc-100" }, segs,
      h("div", { class: "absolute inset-y-0 border-l-2 border-zinc-900", style: { left: pct(ceiling) }, title: `PO ceiling ${money(ceiling, cur)}` }),
      h("div", { class: "absolute inset-y-0 border-l-2 border-dashed border-amber-500", style: { left: pct(ceiling + c.tolerance_minor) },
                 title: `Tolerance ${money(c.tolerance_minor, cur)}` })),
    h("div", { class: "relative mt-1 h-4" },
      h("span", { class: "mono absolute top-0 -translate-x-full whitespace-nowrap pr-1.5 text-[11px] text-zinc-700", style: { left: pct(ceiling) } },
        `PO ${money(ceiling, cur)}`)),
    h("dl", { class: "mt-3 grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm" }, legend.map(([sw, label, val]) => [
      h("dt", { class: "flex items-center gap-2 text-zinc-600" }, h("span", { class: `inline-block h-3 w-3 rounded-sm ${sw}` }), label),
      h("dd", { class: "mono text-right text-zinc-900" }, val)])),
    h("p", { class: `mt-4 text-sm ${overTol ? "font-medium text-rose-800" : "text-zinc-600"}` },
      overTol
        ? `Cumulatively ${money(after, cur)} against an authorised ${money(ceiling, cur)}: ${money(over, cur)} over, beyond the ${money(c.tolerance_minor, cur)} tolerance.`
        : `${money(after, cur)} of ${money(ceiling, cur)} used after this invoice; ${money(Math.max(0, ceiling - after), cur)} left.`));
}

// ---- Edge 3: suspected duplicate, side by side

function duplicatePanel(record, ctx) {
  const inv = record.invoice || {};
  const cur = inv.currency || "INR";
  const dups = record.duplicates;
  const orig = (dups.near && dups.near[0]) || dups.exact[0];
  const rows = [
    ["Invoice number", inv.invoice_number, orig.invoice_number],
    ["Invoice date", inv.invoice_date, orig.invoice_date],
    ["Amount", money(inv.grand_total_minor, cur), orig.total_minor != null ? money(orig.total_minor, cur) : undefined],
    ["PO", record.run.matched_po_number, orig.po_number],
    ["Vendor", record.run.matched_vendor_id, record.run.matched_vendor_id],
    ["Status", "this run", orig.status || orig.decision],
  ].filter(([, , b]) => b !== undefined);
  const resolved = record.card && record.card.resolution;
  return panel(`Possible re-issue of ${orig.invoice_number || "an earlier invoice"}`, "alert", "amber",
    h("div", { class: "overflow-hidden rounded-lg border border-amber-200 bg-white" },
      h("table", { class: "w-full text-left text-sm" },
        h("thead", { class: "bg-zinc-50 text-xs text-zinc-500" },
          h("tr", {}, ["", "This invoice", "Suspected original", ""].map((c) => h("th", { class: "px-4 py-2 font-medium" }, c)))),
        h("tbody", { class: "divide-y divide-zinc-100" }, rows.map(([label, a, b]) => {
          const same = String(a ?? "") === String(b ?? "");
          const neutral = label === "Status";
          return h("tr", { class: !same && !neutral ? "bg-amber-50" : "" },
            h("td", { class: "px-4 py-2.5 text-zinc-500" }, label),
            h("td", { class: "mono px-4 py-2.5 text-zinc-900" }, a ?? "—"),
            h("td", { class: "mono px-4 py-2.5 text-zinc-900" }, b ?? "—"),
            h("td", { class: "px-4 py-2.5 text-right text-xs" }, neutral ? "" : same
              ? h("span", { class: "inline-flex items-center gap-1 text-zinc-500" }, icon("check"), "same")
              : h("span", { class: "inline-flex items-center gap-1 font-medium text-amber-800" }, icon("alert"), "differs")));
        })))),
    h("p", { class: "mt-3 text-xs text-zinc-500" },
      "Same vendor, same amount, days apart, new number. The ERP archive keeps invoice headers only, so line items are compared against the PO instead."),
    !resolved && ctx.record && record.card && record.card.override_roles.includes(getRole())
      ? h("div", { class: "mt-4 flex justify-end gap-2" },
          h("button", { type: "button", class: "focus-ring rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm font-medium text-zinc-800 hover:bg-zinc-50",
                        onclick: () => openAction("override_approve", ctx, { notePlaceholder: "e.g. Vendor confirmed INV-SH-7781 was voided; credit note received" }) },
            "Approve as legitimate re-issue"),
          h("button", { type: "button", class: "focus-ring rounded-lg border border-rose-200 bg-white px-3 py-2 text-sm font-medium text-rose-700 hover:bg-rose-50",
                        onclick: () => openAction("override_reject", ctx, { notePlaceholder: "e.g. Duplicate of the paid INV-SH-7781" }) },
            "Reject as duplicate"))
      : null);
}

export function mountInsights(host, record, ctx) {
  clear(host);
  for (const p of renderInsights(record, { ...ctx, record })) host.appendChild(p);
}
