#!/usr/bin/env bash
# Moves data/state.json to/from the `state` branch of this repository.
#   scripts/state.sh pull   - download the saved state (fresh start if the branch doesn't exist yet)
#   scripts/state.sh push   - commit data/state.json to the `state` branch if it changed
set -euo pipefail

STATE_FILE="${STATE_FILE:-data/state.json}"
BRANCH="${STATE_BRANCH:-state}"
mkdir -p "$(dirname "$STATE_FILE")"

branch_exists() {
  # exit 0 = exists, 2 = not found, anything else = network/auth problem
  set +e
  git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1
  local rc=$?
  set -e
  if [ $rc -eq 0 ]; then return 0; fi
  if [ $rc -eq 2 ]; then return 1; fi
  echo "::error::Could not query the '$BRANCH' branch (git exit $rc). Refusing to continue with an empty state." >&2
  exit 1
}

case "${1:-}" in
  pull)
    if branch_exists; then
      git fetch --quiet --depth=1 origin "$BRANCH"
      if git cat-file -e FETCH_HEAD:state.json 2>/dev/null; then
        git show FETCH_HEAD:state.json > "$STATE_FILE"
        echo "Loaded state ($(wc -c < "$STATE_FILE") bytes) from branch '$BRANCH'."
      else
        echo "::warning::Branch '$BRANCH' has no state.json; starting fresh."
      fi
    else
      echo "No '$BRANCH' branch yet. This is the first run, so a baseline will be created."
    fi
    ;;
  push)
    if [ ! -s "$STATE_FILE" ]; then
      echo "No state file to save."
      exit 0
    fi
    WT="$(mktemp -d)"
    REPO_DIR="$(pwd)"
    cleanup() { cd "$REPO_DIR"; git worktree remove --force "$WT" >/dev/null 2>&1 || true; git worktree prune; }
    trap cleanup EXIT
    git config user.name  "cruise-monitor[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    if branch_exists; then
      git fetch --quiet --depth=1 origin "$BRANCH"
      git worktree add --quiet --detach "$WT" FETCH_HEAD
    else
      git worktree add --quiet --detach "$WT"
      ( cd "$WT" && git checkout --quiet --orphan "tmp-state-$$" && git rm -rfq . >/dev/null 2>&1 || true )
    fi
    cp "$STATE_FILE" "$WT/state.json"
    cat > "$WT/README.md" <<'EOF'
This branch is written automatically by the cruise reward monitor.
`state.json` holds every cruise reward seen and which alerts were sent.
Don't edit it by hand while the monitor is running.
EOF
    cd "$WT"
    git add state.json README.md
    if git diff --cached --quiet; then
      echo "State unchanged; nothing to commit."
      exit 0
    fi
    git commit --quiet -m "Update monitor state"
    for attempt in 1 2 3; do
      if git push --quiet origin "HEAD:$BRANCH"; then
        echo "State saved to branch '$BRANCH'."
        exit 0
      fi
      echo "Push failed (attempt $attempt); retrying..."
      sleep $((attempt * 3))
    done
    echo "::error::Could not save state after 3 attempts."
    exit 1
    ;;
  *)
    echo "usage: $0 pull|push" >&2
    exit 2
    ;;
esac
