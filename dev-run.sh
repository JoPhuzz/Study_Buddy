#!/bin/bash
# Local dev: borrow the gaming-buddy venv + API key, keep the brain purely local
# (no KNOWLEDGE_TOKEN -> sync disabled; webdata/ persists on disk).
cd "$(dirname "$0")"
ENVFILE="$HOME/Developer/gaming-buddy/.env"
if [ -f "$ENVFILE" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      ANTHROPIC_API_KEY|DEEP_MODEL|FAST_MODEL) export "$k=$v" ;;
    esac
  done < "$ENVFILE"
fi
# 8092: Gaming Buddy Mobile owns 8090, Program Buddy 8091. Overridable so a second
# instance can be smoke-tested without colliding or sharing its brain.
export HOST=127.0.0.1 PORT="${PORT:-8092}" DATA_DIR="${DATA_DIR:-webdata}"
exec "$HOME/Developer/gaming-buddy/.venv/bin/python" -m backend.main
