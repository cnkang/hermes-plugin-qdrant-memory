#!/usr/bin/env bash
set -euo pipefail

attempts=3

if [[ "$#" -ne 1 ]]; then
  echo "::error::Usage: $0 requirements.txt|requirements-local.txt" >&2
  exit 2
fi

requirements_file=$1
case "$requirements_file" in
  requirements.txt|requirements-local.txt) ;;
  *)
    echo "::error::Unsupported Snyk requirements file: $requirements_file" >&2
    exit 2
    ;;
esac

if [[ ! -f "$requirements_file" ]]; then
  echo "::error::Snyk requirements file does not exist: $requirements_file" >&2
  exit 2
fi

log_file=$(mktemp "${RUNNER_TEMP:-/tmp}/snyk-monitor.XXXXXX")
trap 'rm -f "$log_file"' EXIT

for ((attempt = 1; attempt <= attempts; attempt++)); do
  echo "Snyk monitor attempt ${attempt}/${attempts}: ${requirements_file}"

  set +e
  snyk monitor \
    "--file=${requirements_file}" \
    --package-manager=pip \
    --command=.scan-env/bin/python 2>&1 | tee "$log_file"
  status=${PIPESTATUS[0]}
  set -e

  if [[ "$status" -eq 0 ]]; then
    exit 0
  fi

  if ! grep -Eq 'Server returned unexpected error for the monitor request\. Status code: 5[0-9]{2}' "$log_file"; then
    echo "::error::Snyk monitor failed without a retryable HTTP 5xx response."
    exit "$status"
  fi

  if [[ "$attempt" -eq "$attempts" ]]; then
    echo "::error::Snyk monitor API still returns HTTP 5xx after ${attempts} attempts."
    exit "$status"
  fi

  delay=$((attempt * 5))
  echo "::warning::Snyk monitor API returned HTTP 5xx; retrying in ${delay}s."
  sleep "$delay"
done
