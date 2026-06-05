#!/bin/sh
set -e

# If arguments were passed (e.g. "ob login"), run them directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

# Past this point we manage the long-running sync ourselves (retry loop +
# graceful shutdown), so we must not let `set -e` abort on ob's non-zero exit.
set +e

child=""

# Forward stop signals to ob so it can disconnect cleanly and release its sync
# session (avoids the "Another sync instance is already running" race on the
# next boot). Exiting 0 here also stops the retry loop on an intentional stop.
term() {
    echo "Received signal, shutting down ob gracefully..."
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
    exit 0
}
trap term TERM INT

# Remove stale locks left by an unclean exit. obsidian-headless can leave a
# lock *directory* (e.g. .sync.lock), so match dirs too — rm -rf, not -delete.
clean_locks() {
    find /root/.config/obsidian-headless /vault \
        \( -name "*.lock" -o -name ".lock" -o -name "lock" \) \
        -print -exec rm -rf {} + 2>/dev/null || true
}

backoff=5
while true; do
    echo "Cleaning stale Obsidian lock files (if any)..."
    clean_locks

    echo "Starting Obsidian Headless Sync (continuous)..."
    ob sync --continuous --path /vault &
    child=$!
    wait "$child"
    code=$?

    # A graceful stop is handled by term() (exits 0). Reaching here means ob
    # died on its own (e.g. session still held) — back off, then retry.
    echo "ob exited (code $code); retrying in ${backoff}s..."
    sleep "$backoff"
    backoff=$([ "$backoff" -ge 60 ] && echo 60 || echo $((backoff * 2)))
done
