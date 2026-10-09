# {{title}}

Built by the AI software factory from the IT golden path `python-worker`.
Spec, design and plan: `work/{{slug}}/`.

The worker's logic is `worker.handle`; `worker.run(inbox)` consumes messages and acknowledges each one only after
it was handled. The broker is chosen by the design from the tech radar; connect it with an adapter implementing
`worker.Inbox`. `GET /health` (FastAPI) reports that the service is up.

```
uv run pytest -q
uv run uvicorn app.main:app --reload   # http://127.0.0.1:8000/health
```
