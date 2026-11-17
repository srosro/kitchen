#!/usr/bin/env bash
# Reference statusline that combines Claude Code session info (context/model/
# branch) with the kitchen segment from `kitchen statusline-segment`. The sous
# runs it when settings.json has no statusLine of its own; to keep yours, copy
# the rate_limits writer below into it.
input=$(cat)

# Claude Code passes the plan's usage windows here after every reply: kept in
# ~/.claude/usage/rate_limits.json, which kitchen's usage warnings read.
rl=$(printf '%s' "$input" | jq -c 'select((.rate_limits|type)=="object") | {updated_at: (now|todateiso8601)} + (.rate_limits | {five_hour, seven_day} | with_entries(select(.value != null)))' 2>/dev/null)
if [ -n "$rl" ] && [ "$rl" != "null" ]; then
  dir="$HOME/.claude/usage"; mkdir -p "$dir"
  tmp=$(mktemp "$dir/.rate_limits.XXXXXX") && printf '%s\n' "$rl" > "$tmp" && mv -f "$tmp" "$dir/rate_limits.json"
fi

# Context window percentage
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

# Model short name: strip date suffix (e.g. "claude-3-5-sonnet-20241022" -> "claude-3-5-sonnet")
model=$(echo "$input" | jq -r '.model.id // empty' | sed 's/-[0-9]\{8\}$//')

# Git branch (non-blocking, skip optional locks)
branch=$(git -C "$(echo "$input" | jq -r '.workspace.current_dir // empty')" \
  --no-optional-locks branch --show-current 2>/dev/null)

# ANSI colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

# Build a 10-cell bar and pick a color based on usage
build_bar() {
  local pct="${1:-0}"
  local filled=$(echo "$pct" | awk '{ printf "%d", int($1 / 10 + 0.5) }')
  [ "$filled" -gt 10 ] && filled=10
  local empty=$((10 - filled))
  local bar=""
  for i in $(seq 1 "$filled"); do bar="${bar}█"; done
  for i in $(seq 1 "$empty");  do bar="${bar}░"; done
  echo "$bar"
}

if [ -n "$used" ]; then
  bar=$(build_bar "$used")
  pct_int=$(printf "%.0f" "$used")

  if   [ "$pct_int" -ge 80 ]; then color="$RED"
  elif [ "$pct_int" -ge 60 ]; then color="$YELLOW"
  else                              color="$GREEN"
  fi

  ctx_part="${color}${bar} ${pct_int}%${RESET}"
else
  ctx_part="${DIM}context: —${RESET}"
fi

# Assemble line segments, only include non-empty ones
parts=()
parts+=("$ctx_part")
[ -n "$model"  ] && parts+=("${DIM}${model}${RESET}")
[ -n "$branch" ] && parts+=("${DIM}${branch}${RESET}")

# Join with two spaces
out=""
for p in "${parts[@]}"; do
  [ -z "$out" ] && out="$p" || out="$out  $p"
done

printf "%b\n" "$out"

# Kitchen segment (delegates to CLI for canonical formatting). Silent when
# outside a kitchen, so this stays composable in any environment.
kitchen_segment=$(kitchen statusline-segment 2>/dev/null)
[ -n "$kitchen_segment" ] && printf '%s\n' "$kitchen_segment"
