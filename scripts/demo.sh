#!/bin/bash
# Demo: durable memory with the qdrant-memory plugin, in embedded mode.
#
# The script uses a dedicated HERMES_HOME, so it does not touch an existing
# Hermes profile. It installs the pinned release, teaches a preference,
# waits for the background extraction, recalls the preference in a new
# session, and walks through export and scoped deletion.
#
# Requirements:
#   - The Hermes CLI (`hermes`) on PATH.
#   - Ollama running locally, with the embedding model pulled:
#       ollama pull qwen3-embedding:4b
#   - A model configuration in the demo home. When the demo home has none,
#     start one Hermes session in it and complete the model setup:
#       HERMES_HOME=$DEMO_HOME hermes
#
# Usage: bash scripts/demo.sh [DEMO_HOME]
#   Default DEMO_HOME: $HOME/.hermes-qdrant-demo

set -u
DEMO_HOME="${1:-$HOME/.hermes-qdrant-demo}"
RELEASE_REF="d3db971ecfcd9e26caf97908bf4dd093d403f70f"   # v0.1.0 release commit
export HERMES_HOME="$DEMO_HOME"
say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }

command -v hermes >/dev/null || { echo "hermes CLI not found on PATH" >&2; exit 1; }
mkdir -p "$DEMO_HOME"
if ! grep -q '^model:' "$DEMO_HOME/config.yaml" 2>/dev/null; then
  echo "No model configuration in $DEMO_HOME/config.yaml." >&2
  echo "Start one Hermes session in the demo home and complete the model" >&2
  echo "setup, then run this script again:" >&2
  echo "  HERMES_HOME=$DEMO_HOME hermes" >&2
  exit 1
fi

# Chat steps get a hard timeout when GNU coreutils is available.
run_chat() {
  if command -v gtimeout >/dev/null; then gtimeout "$1" hermes chat -q "$2" --reasoning none </dev/null
  else hermes chat -q "$2" --reasoning none </dev/null; fi
}

say "Install the pinned release through the Hermes CLI"
echo "  (non-interactive shell: --yes-deps consents to dependency preparation)"
hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory \
  --ref "$RELEASE_REF" --yes-deps </dev/null
hermes plugins enable qdrant-memory </dev/null
hermes config set memory.provider qdrant-memory
hermes qdrant-memory init
hermes qdrant-memory status

say "Session 1 — tell Hermes a preference (the plugin stores it automatically)"
run_chat 780 "Please remember: my project codename is ORION-7 and my main deploy node is 10.99.0.2."

say "Waiting for the background extraction to land..."
for i in $(seq 1 60); do
  total=$(hermes qdrant-memory list 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['count']['total'])" 2>/dev/null || echo 0)
  if [ "${total:-0}" -gt 0 ]; then echo "  ✓ extraction stored ${total} memories"; break; fi
  echo "  ... waiting ($((i*10))s)"
  sleep 10
done

say "Stored memories — scoped inventory"
hermes qdrant-memory list

say "Session 2 — a fresh session recalls the preference"
run_chat 600 "Search your durable memory: what is my project codename, and which node do I deploy to?"

say "Backend — embedded Qdrant, no separate service"
hermes qdrant-memory stats
ls -la "$HERMES_HOME/qdrant-memory/" 2>/dev/null | head -6

say "Lifecycle — export a portable copy"
hermes qdrant-memory export --output /tmp/qdrant-demo-export.json
head -c 420 /tmp/qdrant-demo-export.json; echo

say "Lifecycle — scoped deletion with verification"
hermes qdrant-memory delete-all --user hermes-user --agent hermes --confirm
hermes qdrant-memory list
say "Done — the memory is gone and the scope is empty."
