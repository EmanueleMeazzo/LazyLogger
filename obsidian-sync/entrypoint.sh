#!/bin/sh
set -e

# If arguments were passed (e.g. "ob login"), run them directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

echo "Cleaning stale Obsidian lock files (if any)..."
find /root/.config/obsidian-headless /vault \
    \( -name "*.lock" -o -name ".lock" -o -name "lock" \) \
    -type f -print -delete 2>/dev/null || true

echo "Starting Obsidian Headless Sync (continuous)..."
exec ob sync --continuous --path /vault
