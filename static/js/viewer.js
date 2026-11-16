// Document viewer: the page image, page thumbnails, the Original/Preprocessed
// toggle, and bounding boxes drawn as absolutely positioned divs scaled from
// the normalised (0-1) bbox of each extracted field.
//
// Linking is two-way: highlight(path) lights a box (driven by hovering a field
// row), and clicking a box calls onSelect(path) so the field list can scroll
// to it.

import { api } from "./api.js";
import { clear, h, icon } from "./ui.js";

export function createViewer({ onSelect } = {}) {
  const state = { runId: null, pageCount: 0, page: 1, variant: "original", hasProcessed: false,
                  fields: [], highlighted: null };

  const title = h("span", { class: "text-sm font-medium text-zinc-900" }, "Document");
  const pageLabel = h("span", { class: "mono text-xs text-zinc-500" });
  const toggle = h("div", { class: "hidden inline-flex rounded-lg border border-zinc-200 p-0.5 text-xs", role: "group",
                            "aria-label": "Image variant" });
  const thumbs = h("div", { class: "hidden w-16 shrink-0 space-y-2" });
  const img = h("img", { class: "block w-full select-none", alt: "", draggable: "false" });
  const overlay = h("div", { class: "absolute inset-0" });
  const frame = h("div", { class: "relative overflow-hidden rounded-lg border border-zinc-200 bg-white" }, img, overlay);
  const placeholder = h("div", { class: "flex aspect-[1/1.414] items-center justify-center rounded-lg border border-dashed border-zinc-200 bg-zinc-50 px-6 text-center text-sm text-zinc-500" },
    "The document appears here once it has been read.");
  const stage = h("div", { class: "min-w-0 flex-1" }, placeholder);
  const legend = h("p", { class: "mt-3 hidden text-xs text-zinc-500" });

  const root = h("section", { class: "rounded-xl border border-zinc-200 bg-white p-6" },
    h("div", { class: "mb-4 flex items-center justify-between gap-3" },
      h("div", { class: "flex items-center gap-2" }, icon("file", "text-zinc-400"), title, pageLabel), toggle),
    h("div", { class: "flex gap-3" }, thumbs, stage),
    legend);

  function variantButton(key, label) {
    return h("button", {
      type: "button", class: "variant-btn rounded-md px-2.5 py-1 focus-ring", dataset: { variant: key },
      onclick: () => { state.variant = key; render(); },
    }, label);
  }

  function render() {
    if (!state.runId || !state.pageCount) return;
    if (placeholder.isConnected) stage.replaceChild(frame, placeholder);
    pageLabel.textContent = state.pageCount > 1 ? `page ${state.page} of ${state.pageCount}` : "";
    img.src = api.pageUrl(state.runId, state.page, state.variant);
    img.alt = `Page ${state.page} of the invoice (${state.variant})`;

    toggle.classList.toggle("hidden", !state.hasProcessed);
    for (const b of toggle.querySelectorAll(".variant-btn")) {
      const on = b.dataset.variant === state.variant;
      b.className = `variant-btn rounded-md px-2.5 py-1 focus-ring transition-colors ${on ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"}`;
      b.setAttribute("aria-pressed", String(on));
    }

    clear(thumbs);
    thumbs.classList.toggle("hidden", state.pageCount < 2);
    if (state.pageCount > 1) {
      for (let n = 1; n <= state.pageCount; n++) {
        thumbs.appendChild(h("button", {
          type: "button", "aria-label": `Show page ${n}`,
          class: `block w-full overflow-hidden rounded border focus-ring ${n === state.page ? "border-indigo-600" : "border-zinc-200"}`,
          onclick: () => { state.page = n; render(); },
        }, h("img", { src: api.pageUrl(state.runId, n, "original"), alt: "", class: "block w-full" })));
      }
    }
    drawBoxes();
  }

  function drawBoxes() {
    clear(overlay);
    // Boxes are in original-page coordinates (S03 undoes its deskew and
    // upscale), so they are drawn on the Original variant only.
    const show = state.variant === "original";
    const onPage = state.fields.filter((f) => f.bbox && (f.page || 1) === state.page);
    legend.classList.toggle("hidden", !state.fields.length);
    const located = state.fields.filter((f) => f.bbox).length;
    legend.textContent = show
      ? `${located} of ${state.fields.length} fields located on the page. Click a box to find its field.`
      : "This is what OCR read: deskewed, de-shadowed and binarised. Boxes are drawn on the original.";
    if (!show) return;
    for (const f of onPage) {
      const [x0, y0, x1, y1] = f.bbox;
      const box = h("button", {
        type: "button", class: "bbox focus-ring", dataset: { path: f.field_path },
        title: `${f.field_path}: ${f.value_text ?? "not read"}`,
        "aria-label": `Field ${f.field_path}`,
        style: { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` },
        onclick: () => { highlight(f.field_path); onSelect && onSelect(f.field_path); },
      });
      overlay.appendChild(box);
    }
    if (state.highlighted) highlight(state.highlighted);
  }

  function highlight(path) {
    state.highlighted = path;
    for (const b of overlay.querySelectorAll(".bbox")) b.classList.toggle("bbox-active", b.dataset.path === path);
    const f = state.fields.find((x) => x.field_path === path && x.bbox);
    if (f && (f.page || 1) !== state.page) { state.page = f.page; render(); return; }
    if (f && state.variant !== "original") { state.variant = "original"; render(); }
  }

  return {
    el: root,
    setDocument({ runId, pageCount, hasProcessed = false }) {
      const changed = runId !== state.runId;
      Object.assign(state, { runId, pageCount, hasProcessed });
      if (changed) { state.page = 1; state.variant = "original"; }
      if (!toggle.childElementCount) toggle.append(variantButton("original", "Original"), variantButton("processed", "Preprocessed"));
      render();
    },
    setUnavailable(message) {
      clear(placeholder).append(message);
      if (frame.isConnected) stage.replaceChild(placeholder, frame);
    },
    setFields(fields) { state.fields = fields || []; drawBoxes(); },
    highlight,
    unhighlight() { state.highlighted = null; for (const b of overlay.querySelectorAll(".bbox")) b.classList.remove("bbox-active"); },
  };
}
