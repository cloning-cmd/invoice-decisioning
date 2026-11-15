// Thin fetch wrappers. Every non-2xx response throws an Error carrying the
// server's message, so callers can show it in the error banner.

async function request(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* non-JSON error body: keep the status line */ }
    throw new Error(message);
  }
  return res.json();
}

export const api = {
  health: () => request("/api/health"),
};
