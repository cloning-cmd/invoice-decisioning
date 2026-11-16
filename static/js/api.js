// Thin fetch wrappers. Every non-2xx response throws an Error carrying the
// server's message, so callers can show it in the error banner.

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    throw new Error(`Could not reach the server (${err.message})`);
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    const text = await res.text();
    if (text) {
      let body = null;
      try { body = JSON.parse(text); } catch (_) { body = null; }   // a non-JSON error body: show it as text
      message = body && body.detail
        ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))
        : `${message}: ${text.slice(0, 200)}`;
    }
    throw new Error(message);
  }
  return res.json();
}

function postJson(path, body) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export const api = {
  health: () => request("/api/health"),
  samples: () => request("/api/samples"),
  upload: (file) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request("/api/runs", { method: "POST", body: form });
  },
  fromSample: (key) => postJson("/api/runs/from-sample", { sample: key }),
  run: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  runs: (params) => request(`/api/runs?${new URLSearchParams(params)}`),
  stats: () => request("/api/stats"),
  vendors: () => request("/api/reference/vendors"),
  po: (num) => request(`/api/reference/pos/${encodeURIComponent(num)}`),
  action: (id, body) => postJson(`/api/runs/${encodeURIComponent(id)}/actions`, body),
  pageUrl: (id, n, variant = "original") =>
    `/api/runs/${encodeURIComponent(id)}/page/${n}?variant=${variant}`,
  exportUrl: (id) => `/api/runs/${encodeURIComponent(id)}/export`,
  eventsUrl: (id) => `/api/runs/${encodeURIComponent(id)}/events`,
};
