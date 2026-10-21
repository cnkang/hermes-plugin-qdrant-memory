#!/usr/bin/env bash
set -euo pipefail

quality_python="${QDRANT_QUALITY_PYTHON:-python3.11}"
quality_venv="${1:-.quality/venv}"
"$quality_python" - <<'PY'
import platform
import sys

if sys.version_info[:2] != (3, 11):
    sys.exit("Model dependency snapshot requires Python 3.11")
if platform.system() != "Linux" or platform.machine() != "x86_64":
    sys.exit("Model dependency snapshot requires Linux x86_64")
PY
"$quality_python" -m venv "$quality_venv"
"$quality_venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r requirements-quality.txt
# Resolve the local Runtime normally, using only the verified installed dependencies.
# A new dependency or build requirement must fail until its lock entry is updated.
"$quality_venv/bin/python" -m pip install --no-index --no-build-isolation '.[remote]'
"$quality_venv/bin/python" -m pip check
