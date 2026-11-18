"""Produkcyjny .env: pisany ze środowiska `production` na GitHubie, łączony na serwerze.

    python3 -I scripts/deploy/envfile.py write <ścieżka>                 # zadanie deploy, na runnerze
    python3 -I scripts/deploy/envfile.py merge <wydanie> <host> <wynik>  # feedsodt-deploy, na serwerze

`write` zamienia każdą zmienną środowiska `production` (VARS_JSON, czyli toJSON(vars)
zadania) w jedną linię pod jej własną nazwą, a potem sekrety z SECRETS, które zadanie
podaje po kolei z nazwy: toJSON(secrets) dałoby krokowi każdy sekret, z tokenem GitHuba
włącznie. Zmienne DEPLOY_* ustawiają samo zadanie i do pliku nie trafiają. Wartości idą
w cudzysłowie z \\, \" i $ ($$) poprzedzonymi ucieczką, więc Compose odczytuje dokładnie
tę wartość. Złamanie linii jest odrzucane.

`merge` składa .env, który wydanie instaluje: klucze .env wydania (standardowe wejście
feedsodt-deploy), które wszystkie muszą być w RELEASE_KEYS, a potem każdy klucz pliku hosta
(/etc/odt/feedsodt-host.env, pisany przez roota), który wygrywa. .env wydania z innym kluczem
jest odrzucany, nie przycinany. Plik hosta trzyma to, co decyduje, co i jak działa na
maszynie: COMPOSE_*, porty, ścieżki, limity pamięci i CPU silników.

Ten sam podział co w backendzie ODT (jego scripts/deploy/envfile.py), uproszczony: żaden
sekret ani zmienna nie są wymagane (domyślne w docker-compose.yml są zarazem produkcyjne,
a bez PKP_PLK_APIKEY nie powstaje tylko TripUpdates kolei).
"""

import json
import os
import sys

# Sekrety środowiska; każdy staje się linią .env pod tą samą nazwą. Zadanie deploy
# w .github/workflows/ci.yml podaje każdy z nazwy: nowy sekret zmienia oba miejsca.
SECRETS = ("PKP_PLK_APIKEY",)
# Co wydanie może ustawić poza sekretami: ustawienia fetchera, które nie zmieniają,
# co i jak działa na maszynie. Cała reszta należy do pliku hosta.
RELEASE_VARIABLES = (
    "FEEDSODT_INTERVAL_DAYS",
    "FEEDSODT_RUN_AT",
    "FEEDSODT_RETRY_MINUTES",
    "FEEDSODT_RUN_ON_START",
    "FEEDSODT_SHAPE_WORKERS",
    "FEEDSODT_SHAPE_GPS_ACCURACY",
    "FEEDSODT_RT_TU_INTERVAL",
    "FEEDSODT_RT_VP_INTERVAL",
)
RELEASE_KEYS = frozenset(RELEASE_VARIABLES + SECRETS)
# Bez COMPOSE_FILE z nakładką produkcyjną Compose budowałby obrazy na serwerze
# i montował config/ z katalogu wdrożenia, którego tam nie ma.
REQUIRED_HOST_KEYS = ("COMPOSE_FILE",)
DEPLOY_PREFIX = "DEPLOY_"


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def quote(name, value):
    if "\n" in value or "\r" in value:
        fail(f"{name} zawiera złamanie linii.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def render(variables, secrets):
    """Treść .env dla zmiennych i sekretów środowiska."""
    lines = []
    for name in sorted(variables):
        if name.startswith(DEPLOY_PREFIX):
            continue
        if name in SECRETS:
            fail(f"{name} jest zmienną; ma być sekretem.")
        lines.append(f"{name}={quote(name, str(variables[name]))}")
    for name in SECRETS:
        value = secrets.get(name, "")
        if value:
            lines.append(f"{name}={quote(name, value)}")
    return "".join(line + "\n" for line in lines)


def parse(text):
    """Klucz -> wartość z .env, tak jak Compose czyta formy używane tu i ręcznie."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value.startswith("'") and "'" in value[1:]:
            value = value[1 : value.index("'", 1)]
        elif value.startswith('"'):
            # Do zamykającego cudzysłowu; po nim może stać komentarz.
            out, chars = [], iter(value[1:])
            for char in chars:
                if char == '"':
                    break
                out.append(next(chars, "") if char == "\\" else char)
            value = "".join(out).replace("$$", "$")
        else:
            value = value.split(" #", 1)[0].strip().replace("$$", "$")
        values[key] = value
    return values


def load(path):
    with open(path, encoding="utf-8") as env_file:
        return parse(env_file.read())


def save(path, text):
    """Zapisuje tekst do pliku czytelnego tylko dla właściciela."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as env_file:
        env_file.write(text)


def write(path):
    variables = json.loads(os.environ.get("VARS_JSON") or "{}")
    text = render(variables, os.environ)
    save(path, text)
    print(f"envfile: {text.count(chr(10))} linii -> {path}")


def merge(release_path, host_path, out_path):
    if not os.path.isfile(host_path):
        fail(f"Brak pliku hosta {host_path}; patrz docs/wdrozenie.md.")
    release, host = load(release_path), load(host_path)
    refused = sorted(set(release) - RELEASE_KEYS)
    if refused:
        fail(
            f"Wydanie nie może ustawić {', '.join(refused)}; ustaw to w {host_path}"
            " na serwerze (docs/wdrozenie.md)."
        )
    for name in REQUIRED_HOST_KEYS:
        if not host.get(name):
            fail(f"{host_path} nie ma {name}.")
    # Składane od nowa z odczytanych wartości: do pliku trafiają tylko sprawdzone klucze.
    text = "".join(f"{name}={quote(name, value)}\n" for name, value in {**release, **host}.items())
    save(out_path, text)


if __name__ == "__main__":
    if sys.argv[1:2] == ["write"] and len(sys.argv) == 3:
        write(sys.argv[2])
    elif sys.argv[1:2] == ["merge"] and len(sys.argv) == 5:
        merge(*sys.argv[2:])
    else:
        fail(__doc__.split("\n\n")[1])
