// Talks to the ATMS API with the operator token and keeps the live stream connected.
const KEY = "atms.token";

export const token = {
  get() { try { return sessionStorage.getItem(KEY) || ""; } catch { return ""; } },
  set(v) { try { sessionStorage.setItem(KEY, v); } catch { /* private window, the token then lives only until reload */ } },
  clear() { try { sessionStorage.removeItem(KEY); } catch { /* nothing stored */ } },
};

export class AuthError extends Error {}

export async function call(path, { method = "GET", body, key } = {}) {
  const headers = { Authorization: `Bearer ${token.get()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401 || res.status === 403) throw new AuthError("This token cannot use the console.");
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const detail = data && (data.error || data.detail);
    throw new Error(typeof detail === "string" ? detail : `Request failed with status ${res.status}`);
  }
  return data;
}

export function newKey() {
  return (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random().toString(16).slice(2);
}

// Calls onMessage for every incident change and onState(true|false) as the link comes and goes.
export function stream({ onMessage, onState, onDenied }) {
  let ws = null, wait = 1000, stopped = false, timer = null, opened = false;

  function open() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${scheme}://${location.host}/v1/stream?token=${encodeURIComponent(token.get())}`);
    opened = false;
    ws.onopen = () => { opened = true; wait = 1000; onState(true); };
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch { /* a malformed frame is skipped */ } };
    ws.onclose = async (e) => {
      onState(false);
      if (stopped) return;
      if (e.code === 4401) { onDenied(); return; }
      if (!opened) {            // a refused handshake looks like any network failure, so ask the API whether the token is the reason
        try { await call("/v1/map"); } catch (err) { if (err instanceof AuthError) { onDenied(); return; } }
        if (stopped) return;
      }
      timer = setTimeout(open, wait);
      wait = Math.min(wait * 2, 15000);
    };
    ws.onerror = () => ws.close();
  }
  open();
  return () => { stopped = true; clearTimeout(timer); if (ws) ws.close(); };
}
