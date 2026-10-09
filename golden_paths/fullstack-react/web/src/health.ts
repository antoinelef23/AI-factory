// IT golden path fullstack-react: the frontend talks to the FastAPI backend through small, testable functions.

export type Health = { status: string; app?: string; env?: string };

type Fetch = (input: string) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

/** Ask the backend whether it is up. `fetchFn` is injectable so the logic is tested without a server. */
export async function fetchHealth(fetchFn: Fetch = fetch): Promise<Health> {
  const response = await fetchFn("/health");
  if (!response.ok) {
    throw new Error(`backend health check failed: HTTP ${response.status}`);
  }
  const body = (await response.json()) as Health;
  if (typeof body.status !== "string") {
    throw new Error("backend health check returned no status");
  }
  return body;
}

export function describe(health: Health): string {
  return health.status === "ok" ? "Backend is up" : `Backend status: ${health.status}`;
}
