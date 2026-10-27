#!/bin/sh
# Deploy new commits from origin/main. Run every 5 minutes by mc-update.timer (see README).
#
# New code goes live only if the image builds and the tests pass in it; otherwise the checkout
# and the image are rolled back and the running version stays. `--force` redeploys even when
# there is nothing new. Output goes to the journal: journalctl -u mc-update
set -eu

# Everything runs from a function, so a `git merge` that changes this file mid-run is harmless.
main() {
    cd "$(dirname "$0")/.."
    exec 9>/run/mc-update.lock
    flock -n 9 || { echo "another update is running"; return 0; }

    git fetch -q origin main
    old=$(git rev-parse HEAD)
    new=$(git rev-parse origin/main)
    if [ "$old" = "$new" ] && [ "${1:-}" != "--force" ]; then
        return 0
    fi
    echo "deploying $(git log -1 --format='%h %s' "$new") (was ${old%"${old#???????}"})"

    git merge -q --ff-only origin/main
    if ! docker compose build -q || ! docker compose run --rm --no-deps -e OPENROUTER_API_KEY= web pytest -q -p no:cacheprovider; then
        echo "build or tests failed: keeping the running version"
        git reset -q --hard "$old"
        docker compose build -q
        return 1
    fi

    docker compose up -d --remove-orphans
    docker image prune -f >/dev/null
    echo "deployed $(git rev-parse --short HEAD)"
}

main "$@"
exit
