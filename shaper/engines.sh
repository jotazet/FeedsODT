#!/bin/sh
# Włącza / wyłącza silniki routujące. Wołane z crona (shaper/engines.crontab),
# żeby silniki działały tylko w nocnym oknie kształtowania — poza nim trzymają
# ok. 2 GB RAM-u, a pracują łącznie ok. minuty co kilka dni. Szczegóły:
# shaper/README.md, „Silniki tylko w nocy".
#
#   shaper/engines.sh start   # przed pierwszym slotem z `shape`
#   shaper/engines.sh stop    # po ostatnim slocie i jego ponowieniach
set -eu

cd "$(dirname "$0")/.."

case "${1:-}" in
    start)
        # --no-build: obraz kolejowy ma pull_policy: build, więc samo `up`
        # przebudowywałoby go przy każdym starcie. Budowanie należy do
        # wdrożenia (`up -d --build`), nie do crona. `up` zamiast `start`
        # podnosi też kontener, którego jeszcze nie ma (np. po `down`).
        exec docker compose --profile shape up -d --no-build railrouting graphhopper-bus
        ;;
    stop)
        exec docker compose --profile shape stop railrouting graphhopper-bus
        ;;
    *)
        echo "użycie: $0 start|stop" >&2
        exit 2
        ;;
esac
