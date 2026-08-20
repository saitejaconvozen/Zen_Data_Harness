// One place that knows how to talk to the harness.
// Every call returns {data, error} rather than throwing: a dashboard that
// blanks out because one panel failed is less useful than one that degrades.
async function get(path) {
  try {
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) return { data: null, error: `HTTP ${response.status}` };
    return { data: await response.json(), error: null };
  } catch (cause) {
    return { data: null, error: String(cause) };
  }
}

export const api = {
  status: () => get("/api/status"),
  conversations: () => get("/api/conversations"),
  conversation: (id) => get(`/api/conversation/${encodeURIComponent(id)}`),
  metrics: (runId) => get(`/api/metrics${runId ? `?run_id=${runId}` : ""}`),
};
