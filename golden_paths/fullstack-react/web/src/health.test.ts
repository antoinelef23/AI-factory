import { describe as suite, expect, it } from "vitest";

import { describe, fetchHealth } from "./health";

const answer = (status: number, body: unknown) => async () => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

suite("fetchHealth", () => {
  it("returns the backend's health when it is up", async () => {
    const health = await fetchHealth(answer(200, { status: "ok", app: "x", env: "dev" }));
    expect(health.status).toBe("ok");
    expect(describe(health)).toBe("Backend is up");
  });

  it("fails clearly when the backend answers an error", async () => {
    await expect(fetchHealth(answer(503, {}))).rejects.toThrow("HTTP 503");
  });

  it("fails clearly when the body has no status", async () => {
    await expect(fetchHealth(answer(200, { app: "x" }))).rejects.toThrow("no status");
  });
});
