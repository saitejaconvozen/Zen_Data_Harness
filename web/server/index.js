// Express front for the Zen harness console.
//
// It serves the built React app and proxies /api to the Python status server.
// Node deliberately does not touch the SQLite stores: Python owns every schema
// here, and a second reader with its own idea of the shape is how a dashboard
// starts quietly disagreeing with the pipeline it reports on.
//
//   PORT           port to listen on            (default 4310)
//   ZEN_API_ORIGIN python status server origin  (default http://127.0.0.1:8899)
//   ZEN_TOKEN      token forwarded upstream; the corpus is restricted data
import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 4310);
const API_ORIGIN = process.env.ZEN_API_ORIGIN || "http://127.0.0.1:8899";
const TOKEN = process.env.ZEN_TOKEN || "";

const app = express();
app.disable("x-powered-by");

app.get("/healthz", (_req, res) => res.json({ ok: true, upstream: API_ORIGIN }));

// Mounted at root with a path filter rather than app.use("/api", ...):
// Express strips a mount prefix before the middleware runs, so the proxy would
// forward /metrics instead of /api/metrics and every call 404s.
app.use(
  createProxyMiddleware({
    pathFilter: "/api",
    target: API_ORIGIN,
    changeOrigin: true,
    // The upstream authenticates with a bearer token. Holding it here rather
    // than in the browser keeps it out of page source and browser history.
    on: {
      proxyReq: (proxyReq) => {
        if (TOKEN) proxyReq.setHeader("Authorization", `Bearer ${TOKEN}`);
      },
    },
  })
);

const dist = path.join(here, "..", "dist");
app.use(express.static(dist, { maxAge: "1h", index: false }));
// SPA fallback: every non-API path renders the app and lets the router decide.
app.get("*", (_req, res) => res.sendFile(path.join(dist, "index.html")));

app.listen(PORT, "127.0.0.1", () => {
  console.log(`zen console on http://127.0.0.1:${PORT} -> ${API_ORIGIN}`);
});
