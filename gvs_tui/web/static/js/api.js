// Minimal fetch wrapper for the gvs web API.

// Pass a body to POST:  api(path, body)   — or explicit opts: api(path, { method, body })
export async function api(path, arg) {
  let method, body;
  if (arg && typeof arg === "object" && ("method" in arg || "body" in arg)) {
    method = arg.method;
    body = arg.body;
  } else {
    body = arg;
  }
  const res = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* keep default */ }
    throw new Error(msg);
  }
  return res.json();
}

export function fmtInt(n) {
  if (n == null) return "0";
  return Number(n).toLocaleString("en-US");
}

export function fmtTokens(n) {
  if (!n) return "0";
  n = Number(n);
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
  return String(n);
}

export function fmtElapsed(sec) {
  if (sec == null || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function fmtAgo(ts) {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
