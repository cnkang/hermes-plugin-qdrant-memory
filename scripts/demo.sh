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
# The script marks the demo home it creates (a `.qdrant-demo-home` file) and
# refuses to modify an existing Hermes home that lacks the marker, because
# the setup steps change the home (plugin install, memory.provider).
#
# Requirements:
#   - The Hermes CLI (`hermes`) on PATH.
#   - `python3` on PATH (the script reads the CLI's JSON output).
#   - `git` on PATH (the plugin installer clones the repository).
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
#   hermes plugins install <url> --ref <sha> [--yes-deps]
#       --yes-deps consents to dependency preparation at install time and is
#       passed only when the CLI supports it. Interactive shells can answer
#       the prompt instead; the minimum supported Hermes prepares
#       dependencies by default.
#   hermes chat -q "<query>" --reasoning none
#       One-shot session with compact output; the reasoning panel stays
#       hidden so the demo transcript stays short.

set -euo pipefail
DEMO_HOME="${1:-$HOME/.hermes-qdrant-demo}"
RELEASE_REF="604793341226e45f04500cd1d53fd668ddf89d63"   # Runtime 0.5.1 release commit
# Local CLI sessions attribute memory to the host's local session identity
# (uid:<os-uid>). The inventory and deletion commands must use the same scope.
SCOPE_USER="uid:$(id -u)"
export HERMES_HOME="$DEMO_HOME"
say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }
fail() { echo "demo step failed: $*" >&2; exit 1; }

command -v hermes >/dev/null || { echo "hermes CLI not found on PATH" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found on PATH" >&2; exit 1; }
MARKER="$DEMO_HOME/.qdrant-demo-home"
if [ -e "$DEMO_HOME" ] && [ -n "$(ls -A "$DEMO_HOME" 2>/dev/null)" ] && [ ! -e "$MARKER" ]; then
  echo "Refusing to modify an existing directory: $DEMO_HOME" >&2
  echo "The demo changes the home it runs in (plugin install, memory.provider)." >&2
  echo "Use a fresh path, or mark this home as a demo home first:" >&2
  echo "  touch \"$MARKER\"" >&2
  exit 1
fi
mkdir -p "$DEMO_HOME"
touch "$MARKER"
if ! grep -q '^model:' "$DEMO_HOME/config.yaml" 2>/dev/null; then
  echo "No model configuration in $DEMO_HOME/config.yaml." >&2
  echo "Start one Hermes session in the demo home and complete the model" >&2
  echo "setup, then run this script again:" >&2
  echo "  HERMES_HOME=$DEMO_HOME hermes" >&2
  exit 1
fi

# Chat steps get a hard timeout (independent of the extraction wait below):
# a timeout command when available (GNU timeout or gtimeout), a bounded wait
# with a process-group kill otherwise.
run_chat() {
  local seconds="$1" query="$2" timeout_cmd=""
  if command -v timeout >/dev/null 2>&1; then timeout_cmd="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then timeout_cmd="gtimeout"; fi
  if [ -n "$timeout_cmd" ]; then
    "$timeout_cmd" "$seconds" hermes chat -q "$query" --reasoning none </dev/null
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
  hermes qdrant-memory list --user "$SCOPE_USER" --agent hermes 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['count']['total'])" 2>/dev/null || echo -1
}

say "Install the pinned release through the Hermes CLI"
install_args=(https://github.com/cnkang/hermes-plugin-qdrant-memory --ref "$RELEASE_REF")
install_help=$(hermes plugins install --help 2>/dev/null || true)
# --yes-deps exists on newer Hermes; older releases prepare dependencies by
# default and reject the unknown flag. --force reinstalls over an existing
# checkout, so retries after a partial run stay deterministic.
if printf '%s' "$install_help" | grep -q -- "--yes-deps"; then install_args+=(--yes-deps); fi
if printf '%s' "$install_help" | grep -q -- "--force"; then install_args+=(--force); fi
hermes plugins install "${install_args[@]}" </dev/null || fail "plugins install"
hermes plugins enable qdrant-memory </dev/null || fail "plugins enable"
# The README's interactive flow uses `hermes memory setup` here; the script
# sets the provider directly instead, which is equivalent for a demo home.
hermes config set memory.provider qdrant-memory || fail "config set memory.provider"
hermes qdrant-memory init --existing use || fail "qdrant-memory init"
hermes qdrant-memory status || fail "qdrant-memory status"
pre=$(memories_total)
if [ "$pre" = "-1" ]; then
  fail "cannot read the demo scope for this host user in $DEMO_HOME"
elif [ "$pre" != "0" ]; then
  fail "the demo scope for this host user is not empty in $DEMO_HOME (count: ${pre}); the demo deletes this scope at the end, so it only runs on an empty scope. Use a fresh demo home."
fi

say "Session 1 — tell Hermes a preference (the plugin stores it automatically)"
# Stop any gateway left by an earlier run so the session starts cleanly and the
# inventory reads are not blocked by a stale writer lease.
hermes gateway stop >/dev/null 2>&1 || true
before=$(memories_total)
[ "$before" -ge 0 ] || fail "cannot read the memory store"
run_chat 780 "Please remember: my project codename is ORION-7 and my main deploy node is 192.0.2.10." || fail "session 1 chat"

say "Waiting for the background extraction to land..."
# A one-shot chat leaves its gateway process serving the demo home; that process
# holds the writer lease until it exits. Let the background extraction commit
# (seconds on the documented setup), then stop the demo home's own gateway so
# the inventory commands can read the store.
sleep 45
hermes gateway stop >/dev/null 2>&1 || true
sleep 5
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
hermes qdrant-memory list --user "$SCOPE_USER" --agent hermes || fail "qdrant-memory list"

say "Session 2 — a fresh session recalls the preference"
# Small local models can occasionally answer before their search lands; retry a
# bounded number of times and show the successful answer.
recall_out=""
for attempt in 1 2 3; do
  # Release the previous attempt's gateway before starting a new session.
  hermes gateway stop >/dev/null 2>&1 || true
  sleep 3
  recall_out=$(run_chat 600 "Search your durable memory: what is my project codename, and which node do I deploy to?") || fail "session 2 chat"
  printf '%s\n' "$recall_out" | grep -q "ORION-7" && break
  [ "$attempt" -lt 3 ] && echo "  ... the recall answer missed the codename; retrying (attempt $((attempt + 1)))"
done
printf '%s\n' "$recall_out"
printf '%s\n' "$recall_out" | grep -q "ORION-7" || fail "the recall did not return the stored codename"
# The recall session's gateway holds the writer lease until it exits; stop the
# demo home's own gateway so the remaining read and lifecycle commands run.
sleep 5
hermes gateway stop >/dev/null 2>&1 || true
sleep 3

say "Backend — embedded Qdrant, no separate service"
hermes qdrant-memory stats || true
ls -la "$HERMES_HOME/qdrant-memory/" 2>/dev/null | head -6 || true

say "Lifecycle — export a portable copy"
export_dir=$(mktemp -d "${TMPDIR:-/tmp}/qdrant-demo-export.XXXXXX")
export_file="$export_dir/export.json"
trap 'rm -rf "$export_dir"' EXIT
hermes qdrant-memory export --output "$export_file" --user "$SCOPE_USER" --agent hermes || fail "qdrant-memory export"
head -c 420 "$export_file" || true; echo
rm -rf "$export_dir"
trap - EXIT

say "Lifecycle — scoped deletion with verification"
# The recall session's gateway can hold the writer lease briefly; stop the demo
# home's own gateway, then retry until the lease releases.
hermes gateway stop >/dev/null 2>&1 || true
del_out=""
for i in $(seq 1 60); do
  del_out=$(hermes qdrant-memory delete-all --user "$SCOPE_USER" --agent hermes --confirm </dev/null 2>&1) && break
  echo "  ... waiting for the writer to release ($((i*10))s)"
  sleep 10
done
printf '%s\n' "$del_out"
remaining=$(memories_total)
[ "$remaining" = "0" ] || fail "the scope is not empty after deletion (${remaining} memories)"
hermes qdrant-memory list --user "$SCOPE_USER" --agent hermes || fail "qdrant-memory list"
hermes gateway stop >/dev/null 2>&1 || true
say "Done — the memory is gone and the scope is empty."
