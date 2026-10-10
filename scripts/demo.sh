#!/bin/bash
# Demo: durable memory with the qdrant-memory plugin, in embedded mode.
#
# The script uses a dedicated HERMES_HOME, so it does not touch an existing
# Hermes profile. It installs the pinned release, teaches a preference,
# waits for the background extraction, recalls the preference in a new
# session, and walks through export and scoped deletion.
#
# The final step deletes the whole demo scope, so the script refuses to run
# when that scope already holds memories. Use a fresh demo home.
#
# Requirements:
#   - The Hermes CLI (`hermes`) on PATH.
#   - Ollama running locally, with the embedding model pulled:
#       ollama pull qwen3-embedding:4b
#   - A model configuration in the demo home. When the demo home has none,
#     start one Hermes session in it and complete the model setup, for
#     example with the default demo home:
#       HERMES_HOME=~/.hermes-qdrant-demo hermes
#
# Usage: bash scripts/demo.sh [DEMO_HOME]
#   Default DEMO_HOME: $HOME/.hermes-qdrant-demo
#
# The script drives Hermes non-interactively:
#   hermes plugins install <url> --ref <sha> --yes-deps
#       --yes-deps consents to dependency preparation at install time.
#       Interactive shells can answer the prompt instead; the minimum
#       supported Hermes prepares dependencies by default.
#   hermes chat -q "<query>" --reasoning none
#       One-shot session with compact output; the reasoning panel stays
#       hidden so the demo transcript stays short.

set -euo pipefail
DEMO_HOME="${1:-$HOME/.hermes-qdrant-demo}"
RELEASE_REF="d3db971ecfcd9e26caf97908bf4dd093d403f70f"   # v0.1.0 release commit
export HERMES_HOME="$DEMO_HOME"
say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }
fail() { echo "demo step failed: $*" >&2; exit 1; }

command -v hermes >/dev/null || { echo "hermes CLI not found on PATH" >&2; exit 1; }
mkdir -p "$DEMO_HOME"
if ! grep -q '^model:' "$DEMO_HOME/config.yaml" 2>/dev/null; then
  echo "No model configuration in $DEMO_HOME/config.yaml." >&2
  echo "Start one Hermes session in the demo home and complete the model" >&2
  echo "setup, then run this script again:" >&2
  echo "  HERMES_HOME=$DEMO_HOME hermes" >&2
  exit 1
fi

# Chat steps get a hard timeout (independent of the extraction wait below):
# gtimeout when available, a bounded wait otherwise.
run_chat() {
  local seconds="$1" query="$2"
  if command -v gtimeout >/dev/null; then
    gtimeout "$seconds" hermes chat -q "$query" --reasoning none </dev/null
    return $?
  fi
  set -m
  hermes chat -q "$query" --reasoning none </dev/null &
  local pid=$!
  set +m
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$seconds" ]; do
    sleep 2; waited=$((waited + 2))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo "chat timed out after ${seconds}s" >&2
    return 124
  fi
  wait "$pid"
}

memories_total() {
  hermes qdrant-memory list --user hermes-user --agent hermes 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['count']['total'])" 2>/dev/null || echo -1
}

say "Install the pinned release through the Hermes CLI"
echo "  (non-interactive shell: --yes-deps consents to dependency preparation)"
hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory \
  --ref "$RELEASE_REF" --yes-deps </dev/null || fail "plugins install"
hermes plugins enable qdrant-memory </dev/null || fail "plugins enable"
# The README's interactive flow uses `hermes memory setup` here; the script
# sets the provider directly instead, which is equivalent for a demo home.
hermes config set memory.provider qdrant-memory || fail "config set memory.provider"
hermes qdrant-memory init --existing use || fail "qdrant-memory init"
hermes qdrant-memory status || fail "qdrant-memory status"
pre=$(memories_total)
if [ "$pre" = "-1" ]; then
  fail "cannot read the demo scope (hermes-user/hermes) in $DEMO_HOME"
elif [ "$pre" != "0" ]; then
  fail "the demo scope (hermes-user/hermes) is not empty in $DEMO_HOME (count: ${pre}); the demo deletes this scope at the end, so it only runs on an empty scope. Use a fresh demo home."
fi

say "Session 1 — tell Hermes a preference (the plugin stores it automatically)"
before=$(memories_total)
[ "$before" -ge 0 ] || fail "cannot read the memory store"
run_chat 780 "Please remember: my project codename is ORION-7 and my main deploy node is 10.99.0.2." || fail "session 1 chat"

say "Waiting for the background extraction to land..."
stored=0
for i in $(seq 1 60); do
  total=$(memories_total)
  if [ "$total" -gt "$before" ]; then
    stored=$((total - before))
    echo "  ✓ stored ${stored} new memories (total ${total})"
    break
  fi
  if [ "$total" -lt 0 ]; then echo "  ... list read failed; retrying ($((i*10))s)"
  else echo "  ... waiting ($((i*10))s)"; fi
  sleep 10
done
[ "$stored" -gt 0 ] || fail "the background extraction stored no new memories within 600s"

say "Stored memories — scoped inventory"
hermes qdrant-memory list --user hermes-user --agent hermes || fail "qdrant-memory list"

say "Session 2 — a fresh session recalls the preference"
run_chat 600 "Search your durable memory: what is my project codename, and which node do I deploy to?" || fail "session 2 chat"

say "Backend — embedded Qdrant, no separate service"
hermes qdrant-memory stats || true
ls -la "$HERMES_HOME/qdrant-memory/" 2>/dev/null | head -6 || true

say "Lifecycle — export a portable copy"
export_file=$(mktemp "${TMPDIR:-/tmp}/qdrant-demo-export.XXXXXX")
hermes qdrant-memory export --output "$export_file" || fail "qdrant-memory export"
head -c 420 "$export_file" || true; echo
rm -f "$export_file"

say "Lifecycle — scoped deletion with verification"
hermes qdrant-memory delete-all --user hermes-user --agent hermes --confirm || fail "qdrant-memory delete-all"
remaining=$(memories_total)
[ "$remaining" = "0" ] || fail "the scope is not empty after deletion (${remaining} memories)"
hermes qdrant-memory list --user hermes-user --agent hermes || fail "qdrant-memory list"
say "Done — the memory is gone and the scope is empty."
