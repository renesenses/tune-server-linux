#!/bin/bash
#
# Tune Server — API & Functional Tests
# Tests every endpoint, every feature, every button action.
#
# Usage: ./scripts/api-test.sh [base_url]
# Default: http://192.168.1.18:8888
#

set -euo pipefail

BASE="${1:-http://192.168.1.18:8888}/api/v1"
PASS=0; FAIL=0; WARN=0; ERRORS=""; WARNINGS=""

green() { echo -e "\033[32m  ✓ $1\033[0m"; PASS=$((PASS + 1)); }
red()   { echo -e "\033[31m  ✗ $1\033[0m"; FAIL=$((FAIL + 1)); ERRORS="$ERRORS\n  ✗ $1"; }
yellow(){ echo -e "\033[33m  ⚠ $1\033[0m"; WARN=$((WARN + 1)); }

api_get() {
    local path="$1" label="$2" expected="${3:-200}"
    local status body
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$BASE$path" 2>/dev/null | tail -c 3)
    if [ "$status" = "$expected" ]; then green "$label (HTTP $status)"
    else red "$label — expected $expected, got $status ($BASE$path)"; fi
}

api_json() {
    local path="$1" label="$2" min_items="${3:-0}"
    local body status
    body=$(curl -s --max-time 15 "$BASE$path" 2>/dev/null)
    status=$?
    if [ $status -ne 0 ]; then red "$label — curl failed"; return; fi
    count=$(echo "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 1)" 2>/dev/null || echo "-1")
    if [ "$count" = "-1" ]; then red "$label — invalid JSON"
    elif [ "$count" -ge "$min_items" ]; then green "$label ($count items)"
    else red "$label — expected >= $min_items items, got $count"; fi
}

api_json_field() {
    local path="$1" label="$2" field="$3"
    local body
    body=$(curl -s --max-time 15 "$BASE$path" 2>/dev/null)
    val=$(echo "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$field','__MISSING__'))" 2>/dev/null || echo "__ERROR__")
    if [ "$val" = "__MISSING__" ] || [ "$val" = "__ERROR__" ]; then red "$label — field '$field' missing"
    else green "$label ($field=$val)"; fi
}

api_post() {
    local path="$1" label="$2" expected="${3:-200}"
    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST --max-time 30 "$BASE$path" 2>/dev/null | tail -c 3)
    if [ "$status" = "$expected" ]; then green "$label (HTTP $status)"
    else red "$label — expected $expected, got $status"; fi
}

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Tune Server — API & Functional Tests"
echo "║  $(date '+%Y-%m-%d %H:%M:%S')"
echo "║  Server: $BASE"
echo "╚══════════════════════════════════════════════════════════════╝"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 1. Server Health ═══"
api_json_field "/system/config" "System config" "version"
api_json_field "/system/scan/status" "Scan status" "scanning"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 2. Library — Stats ═══"
api_json_field "/library/stats" "Library stats" "tracks"
api_get "/library/stats/completeness" "Completeness stats"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 3. Library — Albums ═══"
api_json "/library/albums?limit=10" "Albums list" 1
api_json "/library/albums?limit=5&quality=hires" "Albums Hi-Res filter"
api_json "/library/albums?limit=5&format=flac" "Albums FLAC filter"

# Get first album ID for detail tests
ALBUM_ID=$(curl -s "$BASE/library/albums?limit=1" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 0)" 2>/dev/null || echo "0")
if [ "$ALBUM_ID" != "0" ]; then
    api_get "/library/albums/$ALBUM_ID" "Album detail (id=$ALBUM_ID)"
    api_json "/library/albums/$ALBUM_ID/tracks" "Album tracks" 1
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 4. Library — Artists ═══"
api_json "/library/artists?limit=10" "Artists list" 1

ARTIST_ID=$(curl -s "$BASE/library/artists?limit=1" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 0)" 2>/dev/null || echo "0")
if [ "$ARTIST_ID" != "0" ]; then
    api_get "/library/artists/$ARTIST_ID" "Artist detail (id=$ARTIST_ID)"
    api_json "/library/artists/$ARTIST_ID/albums" "Artist albums" 1
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 5. Library — Tracks ═══"
api_json "/library/tracks?limit=10" "Tracks list" 1

TRACK_ID=$(curl -s "$BASE/library/tracks?limit=1" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 0)" 2>/dev/null || echo "0")
if [ "$TRACK_ID" != "0" ]; then
    api_get "/library/tracks/$TRACK_ID" "Track detail (id=$TRACK_ID)"
    api_get "/library/tracks/$TRACK_ID/audio" "Track audio stream"
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 6. Library — Genres ═══"
api_json "/library/genres" "Genres list" 1

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 7. Library — Browse (Directories) ═══"
api_get "/library/browse" "Browse roots"
FIRST_ROOT=$(curl -s "$BASE/library/browse" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('roots',[{}])[0].get('path',''))" 2>/dev/null || echo "")
if [ -n "$FIRST_ROOT" ]; then
    api_get "/library/browse/dir?path=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$FIRST_ROOT'))")" "Browse directory"
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 8. Library — Artwork ═══"
COVER=$(curl -s "$BASE/library/albums?limit=1" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0].get('cover_path','') if d else '')" 2>/dev/null || echo "")
if [ -n "$COVER" ]; then
    COVER_FILE=$(basename "$COVER")
    api_get "/library/artwork/$COVER_FILE" "Artwork image"
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 9. Search ═══"
api_json "/search?q=pink+floyd&limit=5" "Search 'pink floyd'"
api_json "/search?q=jazz&limit=5" "Search 'jazz'"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 10. Zones ═══"
api_json "/zones" "Zones list"
api_json "/devices" "Devices list"
api_get "/devices/audio" "Audio devices"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 11. Radios ═══"
api_json "/radios" "Radio stations" 1
api_get "/radios/export.m3u" "Export M3U"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 12. Podcasts ═══"
api_json "/podcasts/radiofrance" "Radio France podcasts" 10
api_json "/podcasts/search?q=france+inter" "Podcast search"
# Test episodes
FEED=$(curl -s "$BASE/podcasts/radiofrance" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0].get('feed_url','') if d else '')" 2>/dev/null || echo "")
if [ -n "$FEED" ]; then
    api_json "/podcasts/episodes?feed_url=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$FEED'))")&limit=3" "Podcast episodes" 1
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 13. Playlists ═══"
api_json "/playlists" "Playlists list"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 14. Profiles ═══"
api_json "/profiles" "Profiles list" 1
PROFILE_ID=$(curl -s "$BASE/profiles" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['id'] if d else 0)" 2>/dev/null || echo "0")
if [ "$PROFILE_ID" != "0" ]; then
    api_get "/profiles/$PROFILE_ID" "Profile detail"
    api_get "/profiles/$PROFILE_ID/favorites" "Profile favorites"
fi

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 15. Streaming Services ═══"
api_get "/streaming/services" "Streaming services status"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 16. Network ═══"
api_json "/network/media-servers" "Media servers"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 17. Metadata Manager ═══"
api_get "/metadata/doubtful" "Doubtful albums"
api_get "/metadata/suggestions" "Suggestions"
api_get "/metadata/auto-fix/status" "Auto-fix status"
api_get "/metadata/duplicates" "Duplicates"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 18. Radio Favorites ═══"
api_json "/radio-favorites" "Radio favorites"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 19. System ═══"
api_get "/system/health" "System health"
api_get "/system/config" "System config"

# ═══════════════════════════════════════════════════════
echo ""
echo "═══ 20. Error Handling ═══"
api_get "/library/albums/999999" "Album not found" "404"
api_get "/library/tracks/999999" "Track not found" "404"
api_get "/nonexistent/endpoint" "Unknown endpoint" "404"

# ═══════════════════════════════════════════════════════
TOTAL=$((PASS + FAIL + WARN))
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  RESULTS: $PASS passed, $FAIL failed, $WARN warnings / $TOTAL total"
echo "══════════════════════════════════════════════════════════════"

if [ $FAIL -gt 0 ]; then
    echo -e "\033[31m\n  FAILURES:$ERRORS\033[0m"
    echo -e "\n\033[31m  ❌ TUNE API TESTS FAILED\033[0m"
    exit 1
else
    echo -e "\n\033[32m  ✅ TUNE API TESTS PASSED\033[0m"
    exit 0
fi
