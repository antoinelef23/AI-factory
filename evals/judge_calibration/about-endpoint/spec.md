---
type: spec
feature: about-endpoint
version: 0.1.0
status: draft
---

# Spec: about-endpoint

This spec describes only the change to the existing `ping-service`: the addition of `GET /about`.

## 1. Intent

Add one read-only endpoint `GET /about` to the Service. It returns the service name and the deployed version as JSON, so the business can identify the service and its version with a single request. All existing endpoints and their tests stay unchanged.

**Target KPI:**
- 100% of 20 sequential `GET /about` requests return status 200 and the body `{"name": "ping-service", "version": "0.1.0"}`.
- The existing test suite passes with 0 failures and 0 modified or deleted test cases.

## 2. Glossary

- **Service**: the existing ping-service HTTP application.
- **Existing endpoints**: `GET /health`, `GET /ping` and `GET /version`.
- **About endpoint**: the new endpoint `GET /about`.
- **Tech radar**: the company list of technologies, each with a maturity level.
- **Maturity `mvp`**: the maturity target stated in idea.md. Only technologies allowed at this level may be used.
- **Existing test suite**: all tests present in the repository before this change.
- **JSON body**: the response body, serialized as JSON, with the header `Content-Type: application/json`.

## 3. Invariants

- **INV-1**: Only technologies allowed by the company tech radar at maturity `mvp` are used.
- **INV-2**: Every response from `GET /about` has the header `Content-Type: application/json`.
- **INV-3**: Existing behavior does not change except as this spec states, and the existing tests stay green. The only permitted change is that `GET /about` no longer returns 404.
- **INV-4**: `GET /about` is idempotent and read-only. Two identical consecutive requests return identical status codes and bodies, and no request changes Service state.
- **INV-5**: The `version` field returned by `GET /about` equals the `version` returned by `GET /version`.

## 4. Behaviors

- **BHV-1**: No regression
  - Given the change is applied
  - When the existing test suite runs, unmodified
  - Then 100% of its tests pass, and the responses of the Existing endpoints (status, headers, body) are identical to those before the change

- **BHV-2**: `GET /about` returns the service name and version
  - Given the Service is running
  - When a client sends `GET /about`
  - Then the response status is 200 and the body is exactly `{"name": "ping-service", "version": "0.1.0"}`

- **BHV-3**: Unsupported methods on `/about` are rejected
  - Given the Service is running
  - When a client sends `POST`, `PUT` or `DELETE` to `/about`
  - Then the response status is 405 and the Service state is unchanged

- **BHV-4**: Near-miss paths still return 404
  - Given the Service is running
  - When a client sends `GET /about/` or `GET /abouts`
  - Then the response status is 404 and the body is valid JSON

## 5. Examples

```yaml
- id: EX-1
  covers: BHV-2
  request: GET /about
  response:
    status: 200
    headers: {Content-Type: application/json}
    body: {"name": "ping-service", "version": "0.1.0"}

- id: EX-2
  covers: BHV-3
  request: POST /about
  response:
    status: 405

- id: EX-3
  covers: BHV-4
  request: GET /abouts
  response:
    status: 404

- id: EX-4
  covers: BHV-1
  description: GET /version after the change
  request: GET /version
  response:
    status: 200
    body: {"version": "0.1.0"}

- id: EX-5
  covers: INV-4
  description: two consecutive GET /about requests, 1 second apart
  response_1: {status: 200, body: {"name": "ping-service", "version": "0.1.0"}}
  response_2: {status: 200, body: {"name": "ping-service", "version": "0.1.0"}}
```

## 6. Non-goals

- No change to `GET /health`, `GET /ping` or `GET /version`, nor to their tests.
- No additional fields in the `/about` body beyond `name` and `version`.
- No configuration of the name or version at runtime.
- No authentication, caching, rate limiting or logging for `/about`.
- No endpoint beyond `GET /about`.
- No non-JSON response format.

## 7. Evals

| ID | Type | Description | Covers | Success threshold |
|----|------|-------------|--------|-------------------|
| EVAL-1 | Static check | Every technology used in the change (language, framework, runtime, dependencies) appears in the tech radar at maturity `mvp` | INV-1 | 0 technologies outside the allowed list |
| EVAL-2 | Automated (HTTP test) | `GET /about` returns the `Content-Type: application/json` header | INV-2 | 1 of 1 checks pass |
| EVAL-3 | Automated (regression) | Run the existing test suite unmodified, and compare it with the pre-change test files | INV-3, BHV-1, EX-4 | 100% of existing tests pass and 0 test files modified or deleted |
| EVAL-4 | Automated (HTTP test) | Send `GET /about` 2 times in a row; compare status and body of both responses | INV-4, EX-5 | 1 of 1 pairs identical |
| EVAL-5 | Automated (consistency check) | Compare the `version` of `GET /about` with the `version` of `GET /version` | INV-5 | values equal (`0.1.0`) |
| EVAL-6 | Automated (HTTP test) | `GET /about` returns status 200 and body `{"name": "ping-service", "version": "0.1.0"}` | BHV-2, EX-1 | 1 of 1 checks pass |
| EVAL-7 | Automated (HTTP test) | `POST /about`, `PUT /about` and `DELETE /about` each return status 405 | BHV-3, EX-2 | 3 of 3 checks pass |
| EVAL-8 | Automated (HTTP test) | `GET /about/` and `GET /abouts` each return status 404 with a valid JSON body | BHV-4, EX-3 | 2 of 2 checks pass |
