#!/usr/bin/env bash
# close-stale-bugs.sh — Ping stale bug threads on the mozaiklabs.fr forum.
#
# Lists all open bug threads, checks if the last reply is older than 30 days,
# and posts a reminder message asking the reporter if the issue persists.
#
# Does NOT auto-close threads — closing is manual.
#
# Usage:
#   bash scripts/close-stale-bugs.sh          # normal run
#   DRY_RUN=1 bash scripts/close-stale-bugs.sh  # list stale threads without posting
#
# Intended to run monthly via cron or manually.
#
# Environment overrides (optional):
#   FORUM_API_TOKEN — override the default bearer token
#   STALE_DAYS      — override the 30-day threshold
#   DRY_RUN=1       — list stale threads without posting

set -euo pipefail

#──────────────────────────────────────────────────────────────────────────────
# Config
#──────────────────────────────────────────────────────────────────────────────

FORUM_BASE="https://mozaiklabs.fr/api/v1/forum"
FORUM_TOKEN="${FORUM_API_TOKEN:-5fed36d6029c5c11058925682c77d0a99e49f32c8b0d8d09e96009ba208869cc}"
STALE_DAYS="${STALE_DAYS:-30}"

STALE_MSG="<p>Bonjour, ce bug n'a pas eu d'activité depuis 30 jours. Le problème est-il toujours présent avec la dernière version ?</p><p>Si oui, répondez ici et nous rouvrirons l'investigation. Sinon, ce fil sera marqué comme résolu dans 7 jours.</p>"

#──────────────────────────────────────────────────────────────────────────────
# Helpers
#──────────────────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" &>/dev/null || die "'$1' is required but not found in PATH."
}

# Portable epoch calculation (macOS + Linux)
date_to_epoch() {
    local datestr="$1"
    # Strip trailing Z or timezone offset for compatibility
    datestr="${datestr%%Z}"
    datestr="${datestr%%+*}"
    if date --version &>/dev/null 2>&1; then
        # GNU date (Linux)
        date -d "$datestr" +%s 2>/dev/null || echo 0
    else
        # BSD date (macOS) — expects format like "2026-05-01T12:00:00"
        # Convert ISO 8601 to a format BSD date understands
        local formatted
        formatted=$(echo "$datestr" | sed -E 's/T/ /; s/\.[0-9]+$//')
        date -j -f "%Y-%m-%d %H:%M:%S" "$formatted" +%s 2>/dev/null || \
        date -j -f "%Y-%m-%dT%H:%M:%S" "$datestr" +%s 2>/dev/null || echo 0
    fi
}

now_epoch() {
    date +%s
}

#──────────────────────────────────────────────────────────────────────────────
# Pre-flight
#──────────────────────────────────────────────────────────────────────────────

require_cmd curl
require_cmd grep
require_cmd sed

#──────────────────────────────────────────────────────────────────────────────
# 1. Fetch open bug threads
#──────────────────────────────────────────────────────────────────────────────

echo "Fetching open bug threads..."

THREADS_JSON=$(curl -sf -H "Authorization: Bearer $FORUM_TOKEN" \
    "$FORUM_BASE/threads?type=bug&status=open" 2>&1) || {
    die "Failed to fetch threads from forum API."
}

# Check if we got a valid response
if [ -z "$THREADS_JSON" ] || [ "$THREADS_JSON" = "[]" ]; then
    echo "No open bug threads found."
    exit 0
fi

#──────────────────────────────────────────────────────────────────────────────
# 2. Parse threads and check staleness
#──────────────────────────────────────────────────────────────────────────────

NOW=$(now_epoch)
STALE_THRESHOLD=$((STALE_DAYS * 86400))
PINGED=0
SKIPPED=0

echo "Checking for threads with no activity in ${STALE_DAYS} days..."
echo "---"

# Extract thread slugs and last_activity dates using grep/sed (no jq dependency).
# Expected JSON structure: array of objects with "slug", "title", "last_activity_at" fields.
# We parse one thread at a time by splitting on opening braces.

# Extract individual thread blocks — simplified JSON parsing
# Look for slug + last_activity_at pairs

SLUGS=$(echo "$THREADS_JSON" | grep -oE '"slug"\s*:\s*"[^"]*"' | sed -E 's/"slug"\s*:\s*"([^"]*)"/\1/')
TITLES=$(echo "$THREADS_JSON" | grep -oE '"title"\s*:\s*"[^"]*"' | sed -E 's/"title"\s*:\s*"([^"]*)"/\1/')
DATES=$(echo "$THREADS_JSON" | grep -oE '"last_activity_at"\s*:\s*"[^"]*"' | sed -E 's/"last_activity_at"\s*:\s*"([^"]*)"/\1/')

# If no last_activity_at, try updated_at or created_at
if [ -z "$DATES" ]; then
    DATES=$(echo "$THREADS_JSON" | grep -oE '"updated_at"\s*:\s*"[^"]*"' | sed -E 's/"updated_at"\s*:\s*"([^"]*)"/\1/')
fi

if [ -z "$SLUGS" ]; then
    echo "No thread slugs found in API response."
    echo "Response (truncated): ${THREADS_JSON:0:500}"
    exit 0
fi

# Process in parallel arrays line by line
paste <(echo "$SLUGS") <(echo "$TITLES") <(echo "$DATES") | while IFS=$'\t' read -r slug title last_date; do
    [ -n "$slug" ] || continue

    if [ -z "$last_date" ]; then
        echo "  SKIP: $title (slug=$slug) — no date available"
        continue
    fi

    LAST_EPOCH=$(date_to_epoch "$last_date")
    if [ "$LAST_EPOCH" = "0" ]; then
        echo "  SKIP: $title (slug=$slug) — could not parse date: $last_date"
        continue
    fi

    AGE=$((NOW - LAST_EPOCH))
    AGE_DAYS=$((AGE / 86400))

    if [ "$AGE" -lt "$STALE_THRESHOLD" ]; then
        echo "  OK:   $title (${AGE_DAYS}d old) — not stale"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    echo "  STALE: $title (${AGE_DAYS}d old, slug=$slug)"

    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "         [DRY RUN] Would post reminder."
        continue
    fi

    # Escape the message for JSON
    JSON_BODY=$(printf '%s' "$STALE_MSG" | sed 's/"/\\"/g')
    REPLY_PAYLOAD="{\"body\":\"${JSON_BODY}\"}"

    REPLY_RESPONSE=$(curl -sf -X POST "$FORUM_BASE/threads/${slug}/replies" \
        -H "Authorization: Bearer $FORUM_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$REPLY_PAYLOAD" 2>&1) || {
        echo "         ERROR: Failed to post reply to $slug"
        continue
    }

    echo "         Reminder posted."
    PINGED=$((PINGED + 1))
done

echo "---"
echo "Done. Stale threads pinged: ${PINGED:-0}, active threads skipped: ${SKIPPED:-0}."
