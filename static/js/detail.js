// Screen 3 (#/run/<id>): the same workspace as the live view, rebuilt from the
// database, plus the full rule table grouped by category, the audit trail, and
// the decision record download.

import { api } from "./api.js";
import { createWorkspace, ruleTable } from "./run.js";
import { clear, h, icon, when } from "./ui.js";

const CATEGORIES = [["completeness", "Completeness"], ["integrity", "Integrity"], ["vendor", "Vendor"],
                    ["duplicate", "Duplicates"], ["po", "Purchase order"], ["risk", "Risk"], ["confidence", "Confidence"]];

export function mountDetail(root, runId) {
  clear(root);
  const title = h("h1", { class: "truncate text-xl font-semibold tracking-tight" }, "Run");
  const meta = h("p", { class: "mono mt-1 text-xs text-zinc-500" }, runId);
  const header = h("div", { class: "flex items-end justify-between gap-6" },
    h("div", { class: "min-w-0" },
      h("a", { href: "#/dashboard", class: "focus-ring mb-2 inline-flex items-center gap-1 rounded text-sm text-zinc-500 hover:text-zinc-800" },
        h("span", { class: "rotate-180 inline-flex" }, icon("arrowRight")), "All runs"),
      title, meta),
    h("a", { href: api.exportUrl(runId), download: `decision-${runId}.json`,
             class: "focus-ring inline-flex shrink-0 items-center gap-2 rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm font-medium text-zinc-800 hover:bg-zinc-50" },
      icon("download"), "Download decision record (JSON)"));

  const ws = createWorkspace(runId, {
    onRecord(record, extras) {
      title.textContent = record.run.source_filename || runId;
      meta.textContent = `${runId} · received ${when(record.run.created_at)}`
        + (record.run.duration_ms != null ? ` · decided in ${(record.run.duration_ms / 1000).toFixed(1)} s` : "");
      clear(extras).append(rulesByCategory(record.rules), auditTrail(record.audit));
    },
  });
  root.append(header, ws.el);
  ws.start();
  return () => ws.destroy();
}

function rulesByCategory(rules) {
  const counts = rules.reduce((m, r) => ({ ...m, [r.status]: (m[r.status] || 0) + 1 }), {});
  return h("section", { class: "rounded-xl border border-zinc-200 bg-white p-6" },
    h("div", { class: "flex items-baseline justify-between" },
      h("h2", { class: "text-sm font-semibold text-zinc-900" }, `Controls (${rules.length})`),
      h("p", { class: "mono text-xs text-zinc-500" },
        ["pass", "warn", "fail", "not_applicable"].filter((k) => counts[k]).map((k) => `${counts[k]} ${k.replace("_", " ")}`).join(" · "))),
    rules.length
      ? CATEGORIES.filter(([key]) => rules.some((r) => r.category === key)).map(([key, label]) =>
          h("div", { class: "mt-5" },
            h("h3", { class: "mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500" }, label),
            ruleTable(rules.filter((r) => r.category === key))))
      : h("p", { class: "mt-3 text-sm text-zinc-500" }, "The rules did not run: an earlier stage failed."));
}

const ACTION_WORDS = {
  decision_made: "Decision", override_approve: "Approved by hand", override_reject: "Rejected by hand",
  note_added: "Note", field_supplied: "Supplied a field",
};

function auditTrail(entries) {
  return h("section", { class: "rounded-xl border border-zinc-200 bg-white p-6" },
    h("h2", { class: "text-sm font-semibold text-zinc-900" }, "Audit trail"),
    entries.length
      ? h("ol", { class: "relative mt-5 space-y-5 border-l border-zinc-200 pl-6" }, entries.map((a) => {
          const human = a.actor !== "system";
          let change = null;
          if (a.action === "field_supplied") {
            const from = JSON.parse(a.from_value), to = JSON.parse(a.to_value);
            change = h("p", { class: "mono mt-1 text-sm text-zinc-800" },
              `${to.field}: ${from.value ?? "(not read)"} → ${to.value}`);
          } else if (a.to_value) {
            change = h("p", { class: "mono mt-1 text-sm text-zinc-800" }, a.from_value ? `${a.from_value} → ${a.to_value}` : a.to_value);
          }
          return h("li", { class: "relative" },
            h("span", { class: `absolute -left-[1.95rem] top-1 h-3 w-3 rounded-full border-2 border-white ${human ? "bg-indigo-600" : "bg-zinc-400"}` }),
            h("p", { class: "text-sm" },
              h("span", { class: "font-medium text-zinc-900" }, ACTION_WORDS[a.action] || a.action),
              h("span", { class: "text-zinc-500" }, ` · ${a.actor_name} · ${when(a.created_at)}`)),
            change,
            a.note ? h("p", { class: "mt-1 text-sm text-zinc-600" }, a.action === "decision_made" ? a.note : `“${a.note}”`) : null);
        }))
      : h("p", { class: "mt-3 text-sm text-zinc-500" }, "No entries yet."));
}
