"""{{title}}: service entry point (IT golden path: python-fastapi)."""

import os

from fastapi import FastAPI

app = FastAPI(title="{{title}}")

# Configuration comes from the environment, never from code (golden-path rule).
ENVIRONMENT = os.environ.get("APP_ENV", "dev")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "{{slug}}", "env": ENVIRONMENT}
