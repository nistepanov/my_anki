#!/usr/bin/env bash
# Advance the English deck's model-backed stages without a Claude session.
#
# Each stage has already written its questions as task files; this answers them through the API
# and merges the answers back. Meant for the system crontab, so it assumes nothing about who is
# watching: it takes a lock, logs everything, and never touches Anki — publishing a deck is a
# person's decision, and a cron job cannot make it.
#
#   crontab -e
#   17 * * * * /home/nistepanov/PycharmProjects/my_anki/enrich.sh
#
# Needs ANTHROPIC_API_KEY. Reads MODEL and BATCH from the environment if you want to override.

set -o errexit
set -o nounset
set -o pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${TARGET:-en}"
MODEL="${MODEL:-claude-sonnet-5}"
# Batch submission is half price and nobody is waiting on this. Set BATCH= to ask chunk by
# chunk instead, which answers sooner and costs twice as much.
BATCH="${BATCH---batch}"
LOG="${LOG:-$PROJECT/enrich.log}"
LOCK="$PROJECT/.enrich.lock"
PYTHON="${PYTHON:-$PROJECT/.venv/bin/python}"

# Stage name, its task directory, and the flag its merge step reads answers with.
#
# Pictures are absent on purpose. Choosing one means looking at the candidates, which is not
# something the text API can answer, and the step that consumes the search queries also rewrites
# the review task files — unattended, it would overwrite answers nobody has merged yet.
STAGES=(
  "relations:relations:--from-json"
  "model:llm:--from-json"
  "translations:examples:--from-json"
)

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

main() {
  cd "$PROJECT"

  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    log "no ANTHROPIC_API_KEY in the environment; nothing to do"
    exit 1
  fi

  for stage in "${STAGES[@]}"; do
    IFS=':' read -r name directory merge_flag <<< "$stage"
    local path="data/$TARGET/$directory"
    if [[ ! -d $path ]]; then
      log "$name: no $path, skipping"
      continue
    fi

    log "$name: answering task files in $path"
    if ! "$PYTHON" -m pipeline.llm "$path" --model "$MODEL" $BATCH >> "$LOG" 2>&1; then
      log "$name: answering failed, leaving the merge alone"
      continue
    fi

    # The merge is what makes an answer count, so it runs even when only some chunks came back;
    # what is still missing is simply asked for again on the next run.
    log "$name: merging"
    if ! "$PYTHON" -m pipeline."$name" --target "$TARGET" "$merge_flag" "$path" >> "$LOG" 2>&1; then
      log "$name: merge failed"
    fi
  done

  log "run finished"
}

# A run can outlast the hour it started in, and two of them answering the same task file would
# pay twice for one answer.
exec 9> "$LOCK"
if ! flock --nonblock 9; then
  log "another run holds the lock; exiting"
  exit 0
fi

main
