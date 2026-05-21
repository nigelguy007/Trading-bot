const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

export async function fetcher(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}`);
  return res.json();
}

export async function triggerRun(payload) {
  const res = await fetch(`${BASE}/workflow/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Run error ${res.status}`);
  return res.json();
}
