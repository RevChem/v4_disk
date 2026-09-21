import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/users": "http://127.0.0.1:6757",
      "/plans-editor": "http://127.0.0.1:6757",
      "/reports": "http://127.0.0.1:6757",
      "/proverka": "http://127.0.0.1:6757",
      "/activity": "http://127.0.0.1:6757",
      "/convert": "http://127.0.0.1:6757",
      "/summaries": "http://127.0.0.1:6757",
      "/field-season": "http://127.0.0.1:6757",
      "/works-summary": "http://127.0.0.1:6757",
      "/page-order": "http://127.0.0.1:6757",
      "/interface-settings": "http://127.0.0.1:6757",
      "/health": "http://127.0.0.1:6757",
    },
  },
});
