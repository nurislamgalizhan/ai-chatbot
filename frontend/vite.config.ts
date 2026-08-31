import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev only: proxy API calls to the local backend container.
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: {
    sourcemap: false,
  },
});
