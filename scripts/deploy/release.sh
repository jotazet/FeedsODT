#!/bin/sh
# Wdraża wydanie: obrazy taga (to jeszcze niczego nie zmienia), pliki wydania w katalogu
# wdrożenia, nowy .env, nowe kontenery fetchera i nginx-a, aż oba działają. Każdy błąd
# zatrzymuje go w tym miejscu. feedsodt-deploy uruchamia go z rozpakowanego commitu taga
# (docs/wdrozenie.md):
#
#   FEEDSODT_DIR=/srv/feedsodt sh <wydanie>/scripts/deploy/release.sh vX.Y.Z <nowy .env> <wydanie>
#
# Działa też ręcznie z katalogu wdrożenia, z kompletnym .env (feedsodt-deploy dokłada do
# .env zadania ustawienia pliku hosta); wtedy bez podmiany plików:
#
#   sh scripts/deploy/release.sh vX.Y.Z <nowy .env>
#
# Silników routujących (profil shape) nie uruchamia: działają tylko w nocnym oknie
# z shaper/engines.crontab, a ich następny start bierze już obraz tego wydania.
set -eu

tag="$1"
env_new="$2"
src="${3:-}"
RELEASES_KEEP=3
WAIT_TIMEOUT=600
FETCHER=feedsodt-fetcher
# Czego produkcja potrzebuje z repozytorium; reszta jest w obrazach. Bez całego shaper/:
# w shaper/cache/ leżą zaimportowane grafy routingu, których nie wolno skasować.
DEPLOY_FILES="docker-compose.yml docker-compose.prod.yml .env.example nginx scripts/deploy
shaper/engines.sh shaper/engines.crontab shaper/graphhopper shaper/openrailrouting/config.yml"
STAGING=.release-new

log() { echo "release: $*"; }

if [ -n "$src" ]; then
    src="$(cd "$src" && pwd)"
    cd "${FEEDSODT_DIR:?FEEDSODT_DIR wskazuje katalog wdrożenia}"
else
    cd "$(dirname "$0")/../.."
fi

# Wersja to tag obrazów, które CI wypchnęło dla niej (ghcr.io/...:vX.Y.Z).
if ! printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+'; then
    log "oczekiwano taga wydania vX.Y.Z, jest '$tag'" >&2
    exit 1
fi

# Trwający przebieg trzyma blokadę gtfs/.work/.lock (feedsodt/runner.py). Restart w jego
# trakcie niczego nie psuje (podmiana plików jest atomowa), ale wyrzuca pracę, a źródło
# z kształtowaniem czeka wtedy do następnej nocy. W katalogu wdrożenia nic się jeszcze
# nie zmieniło, więc wystarczy wydać ponownie później. Pytamy w kontenerze, bo tam
# blokada ma właściciela.
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
# Pliki Compose i envfile.py wydania: z rozpakowanego taga, dopóki nie są przeniesione.
release_dir="${src:-.}"
# COMPOSE_FILE z nowego .env, żeby każde polecenie niżej widziało nakładkę produkcyjną.
COMPOSE_FILE="$(python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1])
import envfile
print(envfile.load(sys.argv[2])["COMPOSE_FILE"])
' "$release_dir/scripts/deploy" "$env_new")"
export COMPOSE_FILE
# Z silnikami: ich obraz musi być na miejscu przed nocnym startem z crona. Przed podmianą
# plików, żeby brak obrazu w rejestrze zostawił katalog wdrożenia nietknięty.
(cd "$release_dir" && docker compose --env-file "$env_new" --profile shape pull --quiet)

nginx_before=""
if [ -f nginx/default.conf ]; then nginx_before="$(cksum < nginx/default.conf)"; fi
if [ -n "$src" ]; then
    # Najpierw cała kopia obok (ten sam dysk), dopiero potem podmiana ścieżka po ścieżce:
    # błąd kopiowania (brak ścieżki w wydaniu, pełny dysk) niczego nie rusza.
    rm -rf "$STAGING"
    for path in $DEPLOY_FILES; do
        mkdir -p "$STAGING/$(dirname "$path")"
        cp -R "$src/$path" "$STAGING/$path"
    done
    for path in $DEPLOY_FILES; do
        mkdir -p "$(dirname "$path")"
        rm -rf "$path"
        mv "$STAGING/$path" "$path"
    done
    rm -rf "$STAGING"
fi

install -m 600 "$env_new" .env
# --wait: aż każda usługa działa i przechodzi swój healthcheck; kończy się błędem, gdy
# któraś wyjdzie albo stanie się unhealthy.
if ! docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
    log "nie każda usługa zdążyła być zdrowa w ${WAIT_TIMEOUT} s" >&2
    docker compose ps -a >&2
    exit 1
fi
# nginx montuje pojedynczy plik, więc po podmianie trzyma stary, a `up -d` go nie odtwarza,
# bo definicja usługi się nie zmieniła. Odtwarzamy go tylko przy zmianie: feedy znikają
# wtedy na sekundę.
if [ "$(cksum < nginx/default.conf)" != "$nginx_before" ]; then
    log "zmiana nginx/default.conf: odtwarzam web"
    docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" --force-recreate --no-deps web
fi
log "$tag działa"

# Obrazy najnowszych $RELEASES_KEEP wydań zostają na wypadek powrotu (docs/wdrozenie.md).
for repository in ghcr.io/jotazet/feedsodt ghcr.io/jotazet/feedsodt-railrouting; do
    docker image ls "$repository" --format '{{.Tag}}' | grep -Ex 'v[0-9]+\.[0-9]+\.[0-9]+' \
        | sort -V | head -n "-$RELEASES_KEEP" | sed "s|^|$repository:|" \
        | xargs -r docker image rm > /dev/null || log "nie udało się usunąć starych obrazów $repository"
done
