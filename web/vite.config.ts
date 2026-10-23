import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build goes into the Python package, so `quanty ui` serves it with no Node at runtime.
// `npm run dev` proxies the API to a running `quanty ui` on :8765.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/quanty/ui/dist", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8765", ws: true } },
  },
});
