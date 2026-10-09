"""{{title}}: service entry point (IT golden path: fullstack-react)."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="{{title}}")

# Configuration comes from the environment, never from code (golden-path rule).
ENVIRONMENT = os.environ.get("APP_ENV", "dev")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "{{slug}}", "env": ENVIRONMENT}


# The built frontend (web/dist, made by `npm --prefix web run build` or the Docker image) is served at /.
# Mounted last, so the API routes above keep priority.
_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="web")
