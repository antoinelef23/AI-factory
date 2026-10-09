import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server forwards API calls to the FastAPI backend (`uv run uvicorn app.main:app`, port 8000).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/health": "http://127.0.0.1:8000" } },
  test: { environment: "node" },
});
