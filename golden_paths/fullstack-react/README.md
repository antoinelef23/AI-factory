# {{title}}

Built by the AI software factory from the IT golden path `fullstack-react`: a FastAPI backend (`app/`) and a
React frontend (`web/`, Vite + TypeScript). Spec, design and plan: `work/{{slug}}/`.

```
uv run uvicorn app.main:app --reload       # backend, http://127.0.0.1:8000/health
npm --prefix web ci && npm --prefix web run dev   # frontend, proxies /health to the backend
uv run pytest -q && npm --prefix web test  # both test suites
```

Node 24 LTS is the supported runtime (CI uses it). The production image builds the frontend and FastAPI serves it.
