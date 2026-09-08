#!/bin/bash
# Fast, offline test suite — no API key, no network, no screen.
cd "$(dirname "$0")"
exec "$HOME/Developer/gaming-buddy/.venv/bin/python" -m pytest tests/ "$@"
