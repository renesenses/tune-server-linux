#!/usr/bin/env bash
# release-forum-post.sh — Post a release announcement on the mozaiklabs.fr forum.
#
# Reads the current version from pyproject.toml, generates release notes
# from the git log (commits since previous tag), and creates a pinned
# discussion thread via the forum API.
#
# Idempotent: if a thread whose title contains the version already exists,
# the script exits without posting.
#
# Usage:
#   bash scripts/release-forum-post.sh
#
# Environment overrides (optional):
#   FORUM_API_TOKEN — override the default bearer token
#   DRY_RUN=1      — print the payload without posting

set -euo pipefail

#──────────────────────────────────────────────────────────────────────────────
# Config
#──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

FORUM_BASE="https://mozaiklabs.fr/api/v1/forum"
FORUM_TOKEN="${FORUM_API_TOKEN:-5fed36d6029c5c11058925682c77d0a99e49f32c8b0d8d09e96009ba208869cc}"

#──────────────────────────────────────────────────────────────────────────────
# Helpers
#──────────────────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" &>/dev/null || die "'$1' is required but not found in PATH."
}

#──────────────────────────────────────────────────────────────────────────────
# Pre-flight
#──────────────────────────────────────────────────────────────────────────────

require_cmd curl
require_cmd git
require_cmd grep
require_cmd sed

cd "$REPO_DIR"

PYPROJECT="$REPO_DIR/pyproject.toml"
[ -f "$PYPROJECT" ] || die "pyproject.toml not found at $PYPROJECT"

#──────────────────────────────────────────────────────────────────────────────
# 1. Read version from pyproject.toml
#──────────────────────────────────────────────────────────────────────────────

VERSION=$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/.*"([0-9]+\.[0-9]+\.[0-9]+)".*/\1/')
[ -n "$VERSION" ] || die "Could not extract version from pyproject.toml"

echo "Version: v$VERSION"

#──────────────────────────────────────────────────────────────────────────────
# 2. Idempotency check — does a thread for this version already exist?
#──────────────────────────────────────────────────────────────────────────────

echo "Checking for existing thread..."

EXISTING=$(curl -sf -H "Authorization: Bearer $FORUM_TOKEN" \
    "$FORUM_BASE/threads?type=discussion" 2>/dev/null || echo "")

if echo "$EXISTING" | grep -qi "v${VERSION}" 2>/dev/null; then
    echo "A thread for v$VERSION already exists. Skipping."
    exit 0
fi

#──────────────────────────────────────────────────────────────────────────────
# 3. Generate release notes from git log
#──────────────────────────────────────────────────────────────────────────────

# Find the previous tag (tag before the current HEAD tag, if any)
PREV_TAG=$(git describe --tags --abbrev=0 HEAD~1 2>/dev/null || echo "")
if [ -z "$PREV_TAG" ]; then
    echo "Warning: no previous tag found, using last 20 commits."
    COMMITS=$(git log --oneline -20)
else
    echo "Commits since $PREV_TAG"
    COMMITS=$(git log "${PREV_TAG}..HEAD" --oneline)
fi

[ -n "$COMMITS" ] || die "No commits found for release notes."

# Group commits by conventional-commit prefix
FEATS=""
FIXES=""
OTHERS=""

while IFS= read -r line; do
    # Strip leading hash
    MSG="${line#* }"
    if echo "$MSG" | grep -qiE '^feat(\(|:)'; then
        FEATS="${FEATS}<li>${MSG}</li>"
    elif echo "$MSG" | grep -qiE '^fix(\(|:)'; then
        FIXES="${FIXES}<li>${MSG}</li>"
    else
        OTHERS="${OTHERS}<li>${MSG}</li>"
    fi
done <<< "$COMMITS"

#──────────────────────────────────────────────────────────────────────────────
# 4. Build HTML body
#──────────────────────────────────────────────────────────────────────────────

BODY="<p>La version <strong>v${VERSION}</strong> de Tune est disponible.</p>"

if [ -n "$FEATS" ]; then
    BODY="${BODY}<h3>Nouveautés</h3><ul>${FEATS}</ul>"
fi

if [ -n "$FIXES" ]; then
    BODY="${BODY}<h3>Corrections</h3><ul>${FIXES}</ul>"
fi

if [ -n "$OTHERS" ]; then
    BODY="${BODY}<h3>Autres</h3><ul>${OTHERS}</ul>"
fi

BODY="${BODY}<hr><p>Mise à jour :</p><ul>"
BODY="${BODY}<li><strong>macOS</strong> : le serveur se met à jour automatiquement, ou relancez-le.</li>"
BODY="${BODY}<li><strong>Windows</strong> : téléchargez l'archive depuis <a href=\"https://github.com/renesenses/tune-server-linux/releases/tag/v${VERSION}\">GitHub Releases</a>.</li>"
BODY="${BODY}<li><strong>Linux</strong> : <code>cd /opt/tune-server && git pull && sudo systemctl restart tune</code></li>"
BODY="${BODY}<li><strong>iOS / iPadOS</strong> : disponible via TestFlight.</li>"
BODY="${BODY}</ul>"
BODY="${BODY}<p>N'hésitez pas à signaler tout problème dans un nouveau fil.</p>"

TITLE="Tune v${VERSION} — Notes de version"

#──────────────────────────────────────────────────────────────────────────────
# 5. Build JSON payload (portable — no jq dependency)
#──────────────────────────────────────────────────────────────────────────────

# Escape double quotes and backslashes in body/title for JSON
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

JSON_TITLE=$(json_escape "$TITLE")
JSON_BODY=$(json_escape "$BODY")

PAYLOAD="{\"title\":\"${JSON_TITLE}\",\"body\":\"${JSON_BODY}\",\"type\":\"discussion\",\"pinned\":true}"

#──────────────────────────────────────────────────────────────────────────────
# 6. Post or dry-run
#──────────────────────────────────────────────────────────────────────────────

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo
    echo "=== DRY RUN ==="
    echo "Title: $TITLE"
    echo "Body (HTML):"
    echo "$BODY"
    echo
    echo "Payload:"
    echo "$PAYLOAD"
    exit 0
fi

echo "Posting release thread..."

RESPONSE=$(curl -sf -X POST "$FORUM_BASE/threads" \
    -H "Authorization: Bearer $FORUM_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1) || {
    echo "ERROR: Forum API request failed." >&2
    echo "$RESPONSE" >&2
    exit 1
}

# Try to extract the thread URL from the response
THREAD_SLUG=$(echo "$RESPONSE" | grep -oE '"slug"\s*:\s*"[^"]*"' | head -1 | sed -E 's/.*"slug"\s*:\s*"([^"]*)".*/\1/' || true)

if [ -n "$THREAD_SLUG" ]; then
    THREAD_URL="https://mozaiklabs.fr/forum/${THREAD_SLUG}"
    echo
    echo "Thread created: $THREAD_URL"
else
    echo
    echo "Thread created (could not extract URL from response)."
    echo "Response: $RESPONSE"
fi

echo
echo "Done. Release announcement posted for Tune v${VERSION}."
