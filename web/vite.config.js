import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is served by Express on 4310; the dev server proxies to it so the
// browser sees one origin and the auth cookie behaves the same in dev as prod.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:4310" } },
});
