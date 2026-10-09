# AI factory: human entry points.   Run `just` to list.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

default:
    @just --list

# THE verification command: lint + format check + tests with 100% line and branch coverage (offline, free,
# about two minutes on 16 cores: the tests run in parallel).
check:
    uv run ruff check .
    uv run ruff format --check .
    uv run pytest -q -n auto --cov --cov-report=term-missing:skip-covered --cov-fail-under=100

# Auto-fix lint and format.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# BILLED (about $0.4): can the LLM judge tell a good spec from deliberately degraded ones?
calibrate:
    uv run pytest -q -s -m live --live

# Show the company tech radar.
radar:
    uv run factory radar

# Board of all work items.
board:
    uv run factory board
