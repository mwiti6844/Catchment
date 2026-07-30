import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Loopback only. This dev server renders personal WhatsApp and email content,
// so it must never listen on 0.0.0.0 — same constraint as the internal API it
// talks to. `strictPort` so a busy port fails loudly rather than silently
// moving somewhere unexpected.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      // Proxied so the browser sees one origin and the token never reaches
      // client-side JavaScript in production builds.
      "/internal": {
        target: process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8002",
        changeOrigin: true,
      },
    },
  },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true },
});
