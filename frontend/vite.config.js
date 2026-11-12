import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY || "http://127.0.0.1:6757";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/users": apiTarget,
      "/auth": apiTarget,
      "/plans-editor": apiTarget,
      "/reports": apiTarget,
      "/proverka": apiTarget,
      "/activity": apiTarget,
      "/convert": apiTarget,
      "/summaries": apiTarget,
      "/field-season": apiTarget,
      "/works-summary": apiTarget,
      "/supervisors-report": apiTarget,
      "/field-harvest": apiTarget,
      "/page-order": apiTarget,
      "/interface-settings": apiTarget,
      "/schedule": apiTarget,
      "/audit": apiTarget,
      "/pdf-header-labels": apiTarget,
      "/health": apiTarget,
    },
  },
});
