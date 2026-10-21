#!/usr/bin/env bash
set -euo pipefail

# Use a private process and port. Do not change a system Ollama installation.
quality_root="$RUNNER_TEMP/quality-ollama"
mkdir -p "$quality_root"
read -r quality_version quality_sha < <(.quality/venv/bin/python - <<'PY'
import json
p = json.load(open("tests/quality/model-profiles.json"))["ollama"]
print(p["version"], p["archive_sha256"])
PY
)
if [ ! -x "$quality_root/bin/ollama" ]; then
  curl --fail --location --retry 3 --output "$quality_root/ollama.tar.zst" \
    "https://github.com/ollama/ollama/releases/download/v${quality_version}/ollama-linux-amd64.tar.zst"
  echo "$quality_sha  $quality_root/ollama.tar.zst" | sha256sum --check --strict
  tar --zstd -xf "$quality_root/ollama.tar.zst" -C "$quality_root"
  rm "$quality_root/ollama.tar.zst"
fi
export OLLAMA_HOST=127.0.0.1:19134
export OLLAMA_MODELS="$quality_root/models"
export OLLAMA_CONTEXT_LENGTH=4096
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=2
"$quality_root/bin/ollama" serve > "$quality_root/server.log" 2>&1 &
echo "$!" > "$quality_root/pid"
export QUALITY_OLLAMA_BIN="$quality_root/bin/ollama"
.quality/venv/bin/python - <<'PY'
import os
import runpy
import subprocess
import time
import httpx

module = runpy.run_path("scripts/evaluate_owner_lifecycle.py")
profile = module["model_profile"](os.environ["MODEL_PROFILE"])
with httpx.Client(trust_env=False, timeout=30) as client:
    deadline = time.monotonic() + 60
    while True:
        try:
            version = client.get(profile["ollama_url"] + "/api/version")
            version.raise_for_status()
            break
        except httpx.HTTPError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Test Ollama did not start") from None
            time.sleep(1)
    import json
    expected_version = json.load(open("tests/quality/model-profiles.json"))["ollama"]["version"]
    if version.json()["version"] != expected_version:
        raise RuntimeError("Ollama version differs from the frozen profile")
    for name in profile["local_models"]:
        subprocess.run([os.environ["QUALITY_OLLAMA_BIN"], "pull", name], check=True)
    response = client.get(profile["ollama_url"] + "/api/tags")
    response.raise_for_status()
    module["select_installed_models"](profile, response.json()["models"])
    # Load models before the measured run; provider calls retain their normal timeout.
    for name in profile["local_models"]:
        route, payload = (("/api/embed", {"model": name, "input": "warmup"})
                          if name == profile["embedding"]["model"] else
                          ("/api/generate", {"model": name, "prompt": "Hi", "stream": False,
                                             "options": {"num_predict": 1}}))
        client.post(profile["ollama_url"] + route, json=payload, timeout=300).raise_for_status()
    loaded = client.get(profile["ollama_url"] + "/api/ps")
    loaded.raise_for_status()
    # Ollama reports loaded-model size; this is not a peak process RSS measurement.
    resources = {
        "ollama_version": version.json()["version"],
        "loaded_models": [
            {key: row.get(key) for key in ("name", "size", "size_vram", "context_length")}
            for row in loaded.json()["models"] if row["name"] in profile["local_models"]
        ],
    }
    print(json.dumps(resources))
    with open(os.path.join(os.environ["RUNNER_TEMP"], "quality-model-resources.json"), "x") as output:
        json.dump(resources, output, indent=2)
PY
