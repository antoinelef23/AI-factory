---
type: spec
feature: ping-service
version: 0.1.0
status: draft
---

# Spec: ping-service

## 1. Intent

Provide a tiny HTTP service that lets a caller check that it is alive (`/ping`), report its version (`/version`), and report its health (`/health`).

**Target KPI:** 100% of requests to `GET /health`, `GET /ping` and `GET /version` return the documented status code and JSON body, measured over a 100-request smoke run with 0 failures. The p95 latency of each endpoint is at most 200 ms under that run, on the reference test environment.

## 2. Glossary

- **Service**: the ping-service HTTP application specified here.
- **Endpoint**: an HTTP method plus path exposed by the Service.
- **Tech radar**: the company technology radar listing technologies and their maturity levels.
- **Maturity `mvp`**: the target maturity level of this feature. Only technologies the tech radar allows at this level may be used.
- **Smoke run**: a sequence of 100 requests, 100% sequential, made against a running Service instance, split evenly across the three endpoints (rounded to the nearest whole request).
- **JSON body**: the response body, serialized as a JSON object whose `Content-Type` header is `application/json`.

## 3. Invariants

- **INV-1**: Only technologies allowed by the company tech radar at maturity `mvp` are used.
- **INV-2**: Every response from `GET /health`, `GET /ping` and `GET /version` has a `Content-Type` header starting with `application/json`.
- **INV-3**: The `version` value returned by `GET /version` equals the `version` field in this spec's frontmatter (`0.1.0`) at the time of release.
- **INV-4**: The three endpoints are read-only: calling any of them 100 times in a row leaves the responses unchanged, with no persisted state and no side effects.
- **INV-5**: Each response body contains exactly the keys listed in the corresponding behavior, with no additional keys.

## 4. Behaviors

- **BHV-1**: `GET /health` returns 200 `{"status": "ok"}`.
  - Given the Service is running
  - When a client sends `GET /health`
  - Then the response status is 200
  - And the JSON body equals `{"status": "ok"}`

- **BHV-2**: `GET /ping` returns the pong body.
  - Given the Service is running
  - When a client sends `GET /ping`
  - Then the response status is 200
  - And the JSON body equals `{"pong": true}`, where `true` is the JSON boolean and not the string `"true"`

- **BHV-3**: `GET /version` returns the version body.
  - Given the Service is running
  - When a client sends `GET /version`
  - Then the response status is 200
  - And the JSON body equals `{"version": "0.1.0"}`, where the value is a string

- **BHV-4**: Unknown paths are rejected.
  - Given the Service is running
  - When a client sends `GET /unknown`
  - Then the response status is 404

- **BHV-5**: Unsupported methods are rejected on the three endpoints.
  - Given the Service is running
  - When a client sends `POST /ping`, `POST /version` or `POST /health`
  - Then the response status is 405
  - And the body of `GET` responses is unaffected for subsequent requests

- **BHV-6**: Query strings are ignored.
  - Given the Service is running
  - When a client sends `GET /ping?x=1`
  - Then the response status is 200
  - And the JSON body equals `{"pong": true}`

## 5. Examples

```yaml
examples:
  - id: EX-1
    covers: BHV-1
    request: "GET /health"
    response:
      status: 200
      headers: { Content-Type: "application/json" }
      body: { status: "ok" }

  - id: EX-2
    covers: BHV-2
    request: "GET /ping"
    response:
      status: 200
      headers: { Content-Type: "application/json" }
      body: { pong: true }

  - id: EX-3
    covers: BHV-3
    request: "GET /version"
    response:
      status: 200
      headers: { Content-Type: "application/json" }
      body: { version: "0.1.0" }

  - id: EX-4
    covers: BHV-4
    request: "GET /unknown"
    response:
      status: 404

  - id: EX-5
    covers: BHV-5
    request: "POST /ping"
    response:
      status: 405

  - id: EX-6
    covers: BHV-6
    request: "GET /ping?x=1"
    response:
      status: 200
      body: { pong: true }

  - id: EX-7
    covers: INV-4
    scenario: "Call GET /version 100 times in a row"
    expected: "All 100 responses are 200 with body {\"version\": \"0.1.0\"}"
```

## 6. Non-goals

- Authentication or authorization on any endpoint.
- Persistence, databases or any stored state.
- Metrics, tracing or structured logging requirements.
- Endpoints other than `/health`, `/ping` and `/version`.
- Rate limiting, caching headers or CORS configuration.
- HTTPS termination, deployment, scaling or high availability.
- Choosing technologies (this is the design's job).
- Request bodies, request parsing or any methods other than `GET` as supported behavior.
- Throughput targets above the 100-request smoke run.

## 7. Evals

| ID | Type | Description | Covers | Success threshold |
|----|------|-------------|--------|-------------------|
| EVAL-1 | Review | Check every technology used in the design and code against the tech radar at maturity `mvp`. | INV-1 | 0 technologies outside the allowed list |
| EVAL-2 | Automated test | For each of the three endpoints, assert the `Content-Type` header starts with `application/json`. | INV-2 | 3 of 3 endpoints pass |
| EVAL-3 | Automated test | Read the version from this spec's frontmatter and assert `GET /version` returns it. | INV-3 | Values equal, 1 of 1 pass |
| EVAL-4 | Automated test | Call each endpoint 100 times in a row and assert all 100 responses are identical per endpoint. | INV-4, EX-7 | 300 of 300 responses identical per endpoint |
| EVAL-5 | Automated test | Assert the set of keys in each response body equals the documented set exactly. | INV-5 | 3 of 3 endpoints pass |
| EVAL-6 | Automated test | Send `GET /health` and assert status 200 and body `{"status": "ok"}`. | BHV-1, EX-1 | 1 of 1 pass |
| EVAL-7 | Automated test | Send `GET /ping` and assert status 200 and body `{"pong": true}` with a JSON boolean. | BHV-2, EX-2 | 1 of 1 pass |
| EVAL-8 | Automated test | Send `GET /version` and assert status 200 and body `{"version": "0.1.0"}` with a string value. | BHV-3, EX-3 | 1 of 1 pass |
| EVAL-9 | Automated test | Send `GET /unknown` and assert status 404. | BHV-4, EX-4 | 1 of 1 pass |
| EVAL-10 | Automated test | Send `POST` to `/ping`, `/version` and `/health` and assert status 405 on each. | BHV-5, EX-5 | 3 of 3 pass |
| EVAL-11 | Automated test | Send `GET /ping?x=1` and assert status 200 and body `{"pong": true}`. | BHV-6, EX-6 | 1 of 1 pass |
| EVAL-12 | Smoke test | Run the 100-request smoke run and measure failures and p95 latency per endpoint. | Target KPI | 0 failures; p95 ≤ 200 ms |
