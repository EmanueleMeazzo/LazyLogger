#!/bin/sh
set -e

# If arguments were passed (e.g. "ob login"), run them directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

echo "Starting Obsidian Headless Sync (continuous)..."
exec ob sync --continuous --path /vault
