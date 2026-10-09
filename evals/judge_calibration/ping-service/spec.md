---
type: spec
feature: ping-service
version: 0.1.0
status: draft
---

# Spec: ping-service

## 1. Intent

Provide a tiny HTTP service with two endpoints: `GET /ping` returns `{"pong": true}` and `GET /version` returns `{"version": "0.1.0"}`. Like every app of the factory, it also answers `GET /health`.

**Target KPI:** every behavior below passes its eval.

## 2. Glossary

- **Service**: the ping-service HTTP application specified here.
- **Tech radar**: the company technology radar listing technologies and their maturity levels.
- **Maturity `mvp`**: the target maturity level of this feature. Only technologies the tech radar allows at this level may be used.

## 3. Invariants

- **INV-1**: Only technologies allowed by the company tech radar at maturity `mvp` are used.

## 4. Behaviors

- **BHV-1**: `GET /health` returns 200 `{"status": "ok"}`.
  - Given the Service is running
  - When a client sends `GET /health`
  - Then the response status is 200
  - And the JSON body has `status` equal to `"ok"` (other fields allowed)

- **BHV-2**: `GET /ping` returns the pong body.
  - Given the Service is running
  - When a client sends `GET /ping`
  - Then the response status is 200
  - And the JSON body equals `{"pong": true}`, where `true` is the JSON boolean

- **BHV-3**: `GET /version` returns the version body.
  - Given the Service is running
  - When a client sends `GET /version`
  - Then the response status is 200
  - And the JSON body equals `{"version": "0.1.0"}`, where the value is a string

## 5. Examples

```yaml
examples:
  - id: EX-1
    covers: BHV-1
    request: "GET /health"
    response:
      status: 200
      body: { status: "ok" }

  - id: EX-2
    covers: BHV-2
    request: "GET /ping"
    response:
      status: 200
      body: { pong: true }

  - id: EX-3
    covers: BHV-3
    request: "GET /version"
    response:
      status: 200
      body: { version: "0.1.0" }
```

## 6. Non-goals

- Authentication or authorization on any endpoint.
- Persistence, databases or any stored state.
- Endpoints or methods other than `GET /health`, `GET /ping` and `GET /version`.
- Performance, throughput or latency targets.
- Choosing technologies (this is the design's job).

## 7. Evals

| ID | Type | Description | Covers | Success threshold |
|----|------|-------------|--------|-------------------|
| EVAL-1 | Review | Check every technology used in the design and code against the tech radar at maturity `mvp`. | INV-1 | 0 technologies outside the allowed list |
| EVAL-2 | Automated test | Send `GET /health` and assert status 200 and `status` equal to `"ok"`. | BHV-1, EX-1 | 1 of 1 pass |
| EVAL-3 | Automated test | Send `GET /ping` and assert status 200 and body `{"pong": true}` with a JSON boolean. | BHV-2, EX-2 | 1 of 1 pass |
| EVAL-4 | Automated test | Send `GET /version` and assert status 200 and body `{"version": "0.1.0"}` with a string value. | BHV-3, EX-3 | 1 of 1 pass |
