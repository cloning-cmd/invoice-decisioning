// Shared UI helpers: element builder, money and number formatting, icons,
// status styles, the error banner. No framework; values are always inserted
// as text nodes, never as HTML.

// h("div", {class: "x", onclick: fn}, "text", child, [more children])
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

// ---------------------------------------------------------------- formatting

const SYMBOLS = { INR: "₹", EUR: "€", USD: "$", GBP: "£" };

// Money arrives as integer minor units; the arithmetic here stays in integers.
export function money(minor, currency = "INR") {
  if (minor === null || minor === undefined) return "—";
  const neg = minor < 0;
  const abs = Math.abs(minor);
  const whole = Math.floor(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  const grouped = whole.toLocaleString(currency === "INR" ? "en-IN" : "en-US");
  const sym = SYMBOLS[currency] ?? `${currency} `;
  return `${neg ? "-" : ""}${sym}${grouped}.${frac}`;
}

export function ms(n) {
  if (n === null || n === undefined) return "";
  return n >= 10000 ? `${(n / 1000).toFixed(1)} s` : `${n.toLocaleString("en-IN")} ms`;
}

export function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function pct(x, digits = 0) {
  return x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;
}

// ---------------------------------------------------------------- icons (16px, stroke 1.5)

const PATHS = {
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  pause: '<path d="M9 6v12M15 6v12"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="1.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2.5"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
  info: '<circle cx="12" cy="12" r="8"/><path d="M12 11v5M12 8v.5"/>',
  minus: '<path d="M6 12h12"/>',
  image: '<rect x="4" y="5" width="16" height="14" rx="1.5"/><path d="M4 16l4.5-4.5 4 4 2.5-2.5L20 18"/><circle cx="15" cy="9.5" r="1.2"/>',
  dot: '<circle cx="12" cy="12" r="3"/>',
  arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 20h16"/>',
  phone: '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>',
};

export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.className = `inline-flex shrink-0 ${cls}`;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${PATHS[name] || ""}</svg>`;
  return span;
}

// ---------------------------------------------------------------- status vocabulary

// Stage status -> dot colour, word, glyph. Colour is never the only signal.
export const STAGE_STATUS = {
  pending: { dot: "bg-zinc-300", text: "text-zinc-400", word: "Pending" },
  running: { dot: "bg-blue-500 pulse-dot", text: "text-blue-600", word: "Running" },
  ok: { dot: "bg-emerald-600", text: "text-emerald-700", word: "OK" },
  warn: { dot: "bg-amber-500", text: "text-amber-700", word: "Warning" },
  failed: { dot: "bg-rose-600", text: "text-rose-700", word: "Failed" },
  skipped: { dot: "bg-zinc-400", text: "text-zinc-400", word: "Skipped" },
};

export const RULE_STATUS = {
  pass: { cls: "bg-emerald-50 text-emerald-700 ring-emerald-200", word: "Pass", glyph: "check" },
  warn: { cls: "bg-amber-50 text-amber-800 ring-amber-200", word: "Warn", glyph: "alert" },
  fail: { cls: "bg-rose-50 text-rose-700 ring-rose-200", word: "Fail", glyph: "x" },
  not_applicable: { cls: "bg-zinc-50 text-zinc-500 ring-zinc-200", word: "n/a", glyph: "minus" },
};

export const VERDICT = {
  AUTO_APPROVE: { badge: "bg-emerald-600 text-white", ring: "border-emerald-200", glyph: "check",
                  chip: "bg-emerald-50 text-emerald-700 ring-emerald-200", short: "Auto-approved" },
  APPROVE_PENDING_SIGNOFF: { badge: "bg-emerald-50 text-emerald-800 ring-1 ring-inset ring-emerald-300",
                             ring: "border-emerald-200", glyph: "clock",
                             chip: "bg-emerald-50 text-emerald-700 ring-emerald-200", short: "Sign-off" },
  HOLD_FOR_REVIEW: { badge: "bg-amber-50 text-amber-900 ring-1 ring-inset ring-amber-300", ring: "border-amber-300",
                     glyph: "pause", chip: "bg-amber-50 text-amber-800 ring-amber-200", short: "Held" },
  REJECT: { badge: "bg-rose-50 text-rose-800 ring-1 ring-inset ring-rose-300", ring: "border-rose-200",
            glyph: "x", chip: "bg-rose-50 text-rose-700 ring-rose-200", short: "Rejected" },
  BLOCK: { badge: "bg-rose-600 text-white", ring: "border-rose-600 border-t-4", glyph: "lock",
           chip: "bg-rose-600 text-white ring-rose-600", short: "Blocked" },
};

export function verdictChip(verdict) {
  if (!verdict) return h("span", { class: "text-xs text-zinc-400" }, "no decision");
  const v = VERDICT[verdict];
  return h("span", { class: `inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${v.chip}` },
    icon(v.glyph), v.short);
}

export function ruleChip(status) {
  const s = RULE_STATUS[status] || RULE_STATUS.not_applicable;
  return h("span", { class: `inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${s.cls}` },
    icon(s.glyph), s.word);
}

export function codeChip(code, tone = "zinc") {
  const tones = {
    zinc: "bg-zinc-100 text-zinc-700 ring-zinc-200",
    rose: "bg-rose-50 text-rose-700 ring-rose-200",
    amber: "bg-amber-50 text-amber-800 ring-amber-200",
  };
  return h("span", { class: `mono inline-flex rounded-md px-2 py-0.5 text-xs ring-1 ring-inset ${tones[tone]}` }, code);
}

export function confidenceBar(value) {
  if (value === null || value === undefined) return h("span", { class: "text-xs text-zinc-400" }, "—");
  const tone = value >= 0.85 ? "bg-emerald-600" : value >= 0.6 ? "bg-amber-500" : "bg-rose-600";
  return h("span", { class: "inline-flex items-center gap-2" },
    h("span", { class: "relative h-1.5 w-14 overflow-hidden rounded-full bg-zinc-100", role: "img",
                "aria-label": `confidence ${value.toFixed(2)}` },
      h("span", { class: `absolute inset-y-0 left-0 ${tone}`, style: { width: `${Math.round(value * 100)}%` } })),
    h("span", { class: "mono text-xs text-zinc-600" }, value.toFixed(2)));
}

// ---------------------------------------------------------------- error banner

export function showError(message) {
  const host = document.getElementById("banners");
  const banner = h("div", { class: "flex items-start gap-3 rounded-xl border border-rose-200 bg-rose-50 px-6 py-4 text-sm text-rose-800 fade-in", role: "alert" },
    icon("alert", "mt-0.5"),
    h("p", { class: "flex-1" }, message),
    h("button", { class: "rounded-md p-1 text-rose-700 hover:bg-rose-100 focus-ring", "aria-label": "Dismiss",
                  onclick: () => banner.remove() }, icon("x")));
  host.appendChild(banner);
}

export function card(...children) {
  return h("section", { class: "rounded-xl border border-zinc-200 bg-white p-6" }, children);
}
