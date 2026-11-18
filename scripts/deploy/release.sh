#!/bin/sh
# Wdraża wydanie z katalogu wdrożenia: obrazy taga (to jeszcze niczego nie zmienia), nowy
# .env, nowe kontenery fetchera, realtime i nginx-a, aż wszystkie działają. Każdy błąd zatrzymuje go w tym
# miejscu. feedsodt-deploy uruchamia go po skopiowaniu plików wydania do katalogu wdrożenia
# (docs/wdrozenie.md); działa też ręcznie stamtąd, z kompletnym .env (feedsodt-deploy
# dokłada do .env zadania ustawienia pliku hosta):
#
#   sh scripts/deploy/release.sh vX.Y.Z <nowy .env>
#
# Silników routujących (profil shape) nie uruchamia: działają tylko w nocnym oknie
# z shaper/engines.crontab, a ich następny start bierze już obraz tego wydania.
set -eu

tag="$1"
env_new="$2"
RELEASES_KEEP=3
WAIT_TIMEOUT=600
FETCHER=feedsodt-fetcher

cd "$(dirname "$0")/../.."
log() { echo "release: $*"; }

# Wersja to tag obrazów, które CI wypchnęło dla niej (ghcr.io/...:vX.Y.Z).
if ! printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+'; then
    log "oczekiwano taga wydania vX.Y.Z, jest '$tag'" >&2
    exit 1
fi

# Trwający przebieg trzyma blokadę gtfs/.work/.lock (feedsodt/runner.py). Restart w jego
# trakcie niczego nie psuje (podmiana plików jest atomowa), ale wyrzuca pracę, a źródło
# z kształtowaniem czeka wtedy do następnej nocy. Nic się jeszcze nie zmieniło, więc
# wystarczy wydać ponownie później. Pytamy w kontenerze, bo tam blokada ma właściciela.
if [ -n "$(docker ps -q --filter "name=^${FETCHER}\$")" ]; then
    if ! docker exec "$FETCHER" python -I -c '
import fcntl, os, sys
path = os.path.join(os.environ.get("FEEDSODT_WORK_DIR") or os.environ["FEEDSODT_OUTPUT_DIR"] + "/.work", ".lock")
if os.path.exists(path):
    try:
        fcntl.flock(os.open(path, os.O_RDONLY), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(3)
'; then
        log "trwa przebieg fetchera; wydaj ponownie, gdy się skończy (docker logs $FETCHER)" >&2
        exit 1
    fi
fi

# Compose bierze tag obrazów z FEEDSODT_VERSION (docker-compose.prod.yml).
printf 'FEEDSODT_VERSION=%s\n' "$tag" >> "$env_new"
# COMPOSE_FILE z nowego .env, żeby każde polecenie niżej widziało nakładkę produkcyjną.
COMPOSE_FILE="$(python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1])
import envfile
print(envfile.load(sys.argv[2])["COMPOSE_FILE"])
' scripts/deploy "$env_new")"
export COMPOSE_FILE
# Z silnikami: ich obraz musi być na miejscu przed nocnym startem z crona.
docker compose --env-file "$env_new" --profile shape pull --quiet

install -m 600 "$env_new" .env
# --wait: aż każda usługa działa i przechodzi swój healthcheck; kończy się błędem, gdy
# któraś wyjdzie albo stanie się unhealthy.
if ! docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
    log "nie każda usługa zdążyła być zdrowa w ${WAIT_TIMEOUT} s" >&2
    docker compose ps -a >&2
    exit 1
fi
log "$tag działa"

# Obrazy najnowszych $RELEASES_KEEP wydań zostają na wypadek powrotu (docs/wdrozenie.md).
for repository in ghcr.io/jotazet/feedsodt ghcr.io/jotazet/feedsodt-railrouting; do
    docker image ls "$repository" --format '{{.Tag}}' | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' \
        | sort -V | head -n "-$RELEASES_KEEP" | sed "s|^|$repository:|" \
        | xargs -r docker image rm > /dev/null || log "nie udało się usunąć starych obrazów $repository"
done
