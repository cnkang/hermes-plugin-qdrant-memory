#!/usr/bin/env bash
set -euo pipefail

quality_python="${QDRANT_QUALITY_PYTHON:-python3.11}"
quality_venv="${1:-.quality/venv}"
"$quality_python" -c 'import sys; assert sys.version_info[:2] == (3, 11), "Model dependency snapshot requires Python 3.11"'
"$quality_python" -m venv "$quality_venv"
"$quality_venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r requirements-quality.txt
# Resolve the local Runtime normally, using only the verified installed dependencies.
# A new dependency or build requirement must fail until its lock entry is updated.
"$quality_venv/bin/python" -m pip install --no-index --no-build-isolation '.[remote]'
"$quality_venv/bin/python" -m pip check
