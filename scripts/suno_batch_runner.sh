#!/usr/bin/env bash
# suno_batch_runner.sh — v0.7.0 hardened batch generation template.
#
# Fixes over legacy per-album runners:
#  R-01 gen-gate MANDATORY before every generate (style+lyrics+artistry in one call;
#       runner physically cannot submit an unguarded prompt).
#  R-02 Library reconciliation: before ANY retry, query `suno list` — if the track
#       already exists remotely (gen succeeded, download failed), download it
#       instead of burning credits on a duplicate generation.
#  R-03 Exponential backoff on Vercel/Code 11 (60s -> 120s -> 240s, cap 600s)
#       instead of a fixed 60s that can thrash against the checkpoint window.
#  R-04 Credit floor check before each track; abort batch cleanly when credits
#       are below the per-track cost instead of failing mid-generation.
#  R-05 Stuck-detection: if the same attempt error repeats 3x, rotate strategy
#       (kill chrome profile, force double auth refresh, extended cooldown)
#       instead of repeating the identical failing call.
#  R-06 Per-track attempt budget + end-of-batch retry round for stragglers, so
#       one bad track cannot stall the whole batch.
#  R-07 Postcheck after download: clip metadata tags must still match the
#       PromptStyle source (v0.6.0 postcheck), else flag for regen.
#
# Usage:
#   suno_batch_runner.sh <jobs.tsv> <base_dir> <album_dir> <playlist_id> [chat_target]
#
# jobs.tsv columns (tab-separated):
#   NUM  TITLE  STYLE_FILE  LYRICS_FILE  ALBUM_FILE  VOCAL  SEARCH_TERM
set -u
cd /root/.openclaw/workspace

JOBS_FILE="${1:?jobs.tsv required}"
BASE="${2:?base dir required}"
ALBUM="${3:?album dir required}"
PLAYLIST="${4:?playlist id required}"
CHAT="${5:-}"

SUNO=./suno-cli/target/release/suno
GUARD=suno-cli/scripts/suno_prompt_guard.py
MLOG="$BASE/BATCH-RUNNER.log"
MAX_ATTEMPTS=8
CREDIT_FLOOR=15
mkdir -p "$ALBUM"
touch "$MLOG"

if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & sleep 3
fi
export DISPLAY=:99
export SUNO_CHROME_PATH=/root/.openclaw/workspace/suno-out/chrome-xvfb.sh

log() { echo "[$(date +%H:%M:%S)] $*" >> "$MLOG"; }

send_tg() {
  [ -z "$CHAT" ] && return 0
  local msg="$1" fp="${2:-}"
  if [ -n "$fp" ] && [ -f "$fp" ]; then
    openclaw message send --target "$CHAT" --message "$msg" --media "$fp" >/dev/null 2>&1 \
      || openclaw message send --target "$CHAT" --message "$msg" >/dev/null 2>&1
  else
    openclaw message send --target "$CHAT" --message "$msg" >/dev/null 2>&1
  fi
}

credits_left() {
  $SUNO credits --json 2>/dev/null | python3 -c "
import sys,json
try: print(json.load(sys.stdin)['data']['credits'])
except Exception: print(-1)"
}

# R-02: reconcile with remote library — returns clip ids if title already generated.
library_ids_for_title() {
  local title="$1" search="$2"
  $SUNO search "$search" --json 2>/dev/null | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin); rows=d.get('data',d) if isinstance(d,dict) else d
except Exception: rows=[]
ids=[s.get('id') for s in rows if s.get('title','')=='''$title''' and s.get('status') in ('complete','streaming')][:2]
print(json.dumps(ids))"
}

reset_browser() { pkill -9 -f 'snap-suno-profile' 2>/dev/null; sleep 2; }

gen_one() {
  local NUM="$1" TITLE="$2" SF="$3" LF="$4" AF="$5" VOCAL="$6" SRCH="$7"
  [ -f "$ALBUM/$AF" ] && { log "SKIP $NUM (album file exists)"; return 0; }

  # R-01: mandatory gen-gate — cannot proceed on failure.
  if ! python3 "$GUARD" gen-gate --title "$TITLE" --style-file "$BASE/$SF" --lyrics-file "$BASE/$LF" >> "$MLOG" 2>&1; then
    log "$NUM GATE-FAIL — prompt rejected, not submitting"
    send_tg "⛔ $NUM \"$TITLE\" fail gen-gate — cần sửa prompt/lyrics, KHÔNG gen."
    return 2
  fi

  # R-04: credit floor.
  local CR; CR=$(credits_left)
  if [ "$CR" -ge 0 ] && [ "$CR" -lt "$CREDIT_FLOOR" ]; then
    log "$NUM ABORT — credits $CR below floor $CREDIT_FLOOR"
    send_tg "⛔ Hết credits (còn $CR). Dừng batch sạch tại $NUM."
    return 3
  fi

  local OUTDIR="$BASE/downloads-$NUM"; mkdir -p "$OUTDIR"
  local TAGS; TAGS="$(cat "$BASE/$SF")"
  local cooldown=60 last_err="" same_err_count=0

  for a in $(seq 1 $MAX_ATTEMPTS); do
    # R-02: check library FIRST — maybe a previous attempt actually succeeded.
    local IDS; IDS=$(library_ids_for_title "$TITLE" "$SRCH")
    if [ "$IDS" != "[]" ]; then
      log "$NUM found in library $IDS — downloading instead of regenerating"
      local dl_ids; dl_ids=$(echo "$IDS" | python3 -c "import sys,json;print(' '.join(json.load(sys.stdin)))")
      $SUNO download $dl_ids --output "$OUTDIR" >> "$MLOG" 2>&1
      ls "$OUTDIR"/*.mp3 >/dev/null 2>&1 && break
    fi

    log "$NUM attempt $a"
    reset_browser
    $SUNO auth --refresh >/dev/null 2>&1
    local OUT
    OUT=$($SUNO generate \
      --title "$TITLE" --tags "$TAGS" \
      --exclude "karaoke, over-singing, harsh pop, autotune, English, profanity" \
      --lyrics-file "$BASE/$LF" \
      --vocal "$VOCAL" --model v5.5 --weirdness 26 --style-influence 82 \
      --wait --download "$OUTDIR" 2>&1)
    echo "$OUT" >> "$MLOG"
    ls "$OUTDIR"/*.mp3 >/dev/null 2>&1 && { log "$NUM SUCCESS"; break; }

    # R-05: stuck detection — same error signature 3x -> strategy rotation.
    local err_sig; err_sig=$(echo "$OUT" | grep -oiE "Vercel|Code 11|captcha|timeout|rate.?limit|schema_drift|auth" | sort -u | tr '\n' ',')
    if [ "$err_sig" = "$last_err" ]; then same_err_count=$((same_err_count+1)); else same_err_count=0; last_err="$err_sig"; fi
    if echo "$OUT" | grep -qiE "schema_drift"; then
      log "$NUM schema_drift -> suno update"; $SUNO update >> "$MLOG" 2>&1
    fi
    if echo "$OUT" | grep -qiE "Vercel|Code 11|Failed to verify your browser"; then
      log "$NUM Vercel checkpoint -> backoff ${cooldown}s"
      reset_browser; sleep "$cooldown"
      cooldown=$(( cooldown * 2 )); [ "$cooldown" -gt 600 ] && cooldown=600   # R-03
      $SUNO auth --refresh >/dev/null 2>&1; sleep 3; $SUNO auth --refresh >/dev/null 2>&1
    elif [ "$same_err_count" -ge 2 ]; then
      log "$NUM stuck ($err_sig x3) -> full strategy rotation: profile wipe + 180s + double refresh"
      reset_browser; sleep 180
      $SUNO auth --refresh >/dev/null 2>&1; sleep 5; $SUNO auth --refresh >/dev/null 2>&1
      same_err_count=0
    else
      sleep 10
    fi
  done

  local FIRST; FIRST=$(ls "$OUTDIR"/*.mp3 2>/dev/null | head -1)
  if [ -z "$FIRST" ]; then
    log "$NUM FAILED after $MAX_ATTEMPTS attempts"
    echo "$NUM" >> "$BASE/RETRY-QUEUE.txt"   # R-06
    send_tg "⚠️ $NUM \"$TITLE\" chưa xong sau $MAX_ATTEMPTS lần — vào retry queue cuối batch."
    return 1
  fi
  cp "$FIRST" "$ALBUM/$AF"

  # Playlist add + R-07 postcheck.
  local JWT; JWT=$(python3 -c "import json;d=json.load(open('/root/.config/suno-cli/auth.json'));print(d.get('jwt',''))")
  local IDS2; IDS2=$(library_ids_for_title "$TITLE" "$SRCH")
  if [ "$IDS2" != "[]" ]; then
    curl -s -o /dev/null -w "$NUM PLAYLIST_HTTP %{http_code}\n" \
      "https://studio-api.prod.suno.com/api/playlist/update_clips" \
      -H "Authorization: Bearer $JWT" -H "Origin: https://suno.com" -H "Referer: https://suno.com/" \
      -H "User-Agent: Chrome/144" -H "Content-Type: application/json" \
      -d "{\"playlist_id\":\"$PLAYLIST\",\"update_type\":\"add\",\"metadata\":{\"clip_ids\":$IDS2}}" >> "$MLOG" 2>&1
    local first_id; first_id=$(echo "$IDS2" | python3 -c "import sys,json;print(json.load(sys.stdin)[0])")
    python3 "$GUARD" postcheck "$first_id" --suno "$SUNO" >> "$MLOG" 2>&1 \
      || log "$NUM POSTCHECK-MISMATCH — metadata drifted from PromptStyle, review before delivery"
  fi
  send_tg "🎵 $NUM \"$TITLE\" ($VOCAL) — XONG ✅" "$ALBUM/$AF"
  return 0
}

: > "$BASE/RETRY-QUEUE.txt"
while IFS=$'\t' read -r NUM TITLE SF LF AF VOCAL SRCH; do
  [ -z "${NUM:-}" ] && continue
  case "$NUM" in \#*) continue;; esac
  gen_one "$NUM" "$TITLE" "$SF" "$LF" "$AF" "$VOCAL" "$SRCH"
  rc=$?
  [ "$rc" -eq 3 ] && break   # credit abort
done < "$JOBS_FILE"

# R-06: one retry round for stragglers.
if [ -s "$BASE/RETRY-QUEUE.txt" ]; then
  log "RETRY ROUND for: $(tr '\n' ' ' < "$BASE/RETRY-QUEUE.txt")"
  sleep 300
  while IFS=$'\t' read -r NUM TITLE SF LF AF VOCAL SRCH; do
    [ -z "${NUM:-}" ] && continue
    case "$NUM" in \#*) continue;; esac
    grep -qx "$NUM" "$BASE/RETRY-QUEUE.txt" || continue
    gen_one "$NUM" "$TITLE" "$SF" "$LF" "$AF" "$VOCAL" "$SRCH"
  done < "$JOBS_FILE"
fi

DONE=$(ls "$ALBUM"/*.mp3 2>/dev/null | wc -l)
TOTAL_JOBS=$(grep -cv '^#' "$JOBS_FILE" 2>/dev/null || echo '?')
log "BATCH COMPLETE: $DONE/$TOTAL_JOBS"
send_tg "🏁 Batch xong: $DONE/$TOTAL_JOBS bài trong $ALBUM"
