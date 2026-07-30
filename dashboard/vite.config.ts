import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const here = dirname(fileURLToPath(import.meta.url));

/**
 * The internal API's shared secret, read at dev-server startup.
 *
 * It is attached by the proxy, in Node, on the way out — so it never reaches
 * client-side JavaScript and never lands in the bundle. Putting it anywhere
 * Vite can inline (`define`, a `VITE_`-prefixed variable) would publish it to
 * every browser that loads the page.
 *
 * Read from the environment first, falling back to the repository `.env` that
 * the rest of the stack already uses. Never a literal in this file.
 */
function internalToken(): string {
  const fromEnv = process.env.CATCHMENT_INTERNAL_API_TOKEN;
  if (fromEnv) return fromEnv;

  try {
    const env = readFileSync(resolve(here, "..", ".env"), "utf8");
    const line = env
      .split("\n")
      .find((entry) => entry.startsWith("CATCHMENT_INTERNAL_API_TOKEN="));
    return line ? line.slice(line.indexOf("=") + 1).trim() : "";
  } catch {
    return "";
  }
}

// Loopback only. This dev server renders personal WhatsApp and email content,
// so it must never listen on 0.0.0.0 — same constraint as the internal API it
// talks to. `strictPort` so a busy port fails loudly rather than silently
// moving somewhere unexpected.
export default defineConfig(() => {
  const token = internalToken();
  if (!token) {
    // Loud, because the alternative is a dashboard where every panel reads
    // "403 forbidden" and the cause is two processes away.
    throw new Error(
      "CATCHMENT_INTERNAL_API_TOKEN is not set (checked the environment and " +
        "../.env). The internal API fails closed, so the dashboard cannot load " +
        "anything without it.",
    );
  }

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        // Proxied so the browser sees one origin and the token never reaches
        // client-side JavaScript.
        "/internal": {
          target: process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8002",
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on("proxyReq", (request) => {
              request.setHeader("X-Internal-Token", token);
            });
          },
        },
      },
    },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true },
  };
});
