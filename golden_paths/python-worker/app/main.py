"""{{title}}: service entry point (IT golden path: python-worker).

One process: GET /health, and the consumer (worker/) running next to it once a broker adapter exists.
"""

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from worker import connect, serve

# Configuration comes from the environment, never from code (golden-path rule).
ENVIRONMENT = os.environ.get("APP_ENV", "dev")
NO_ADAPTER = "no broker adapter yet (worker/adapter.py)"
consumer = {"state": NO_ADAPTER}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    inbox, stop, thread = connect(), threading.Event(), None
    consumer["state"] = NO_ADAPTER
    if inbox is not None:
        thread = threading.Thread(target=serve, args=(inbox, stop), daemon=True)
        thread.start()
        consumer["state"] = "running"
    yield
    stop.set()
    if thread is not None:
        thread.join(timeout=10)
        consumer["state"] = "stopped"


app = FastAPI(title="{{title}}", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "{{slug}}", "env": ENVIRONMENT, "consumer": consumer["state"]}
