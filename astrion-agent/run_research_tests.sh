#!/usr/bin/env bash
# Optional research dependencies required; no model or external service requests.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${ASTRION_TEST_PYTHON:-.venv-ui/bin/python}"
"$PYTHON_BIN" -B -m unittest discover -s tests/research -t . -v
"$PYTHON_BIN" -B -m unittest test_research_api test_research_http
