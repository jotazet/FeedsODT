# FeedsODT

Pobiera rozkłady GTFS z portali przewoźników i organizatorów, które nie mają stałych
linków, przebudowuje je (barwy linii, perony, geometria przejazdu) i wystawia pod
niezmiennymi adresami na `data.odt.org.pl/gtfs/`. Część projektu **Otwarte Dane
Transportowe (ODT)**: katalog ODT i jego planer podróży (OpenTripPlanner) pobierają
stąd kopie feedów co noc.

- **Odbiorcy:** planer ODT i każdy, kto pobiera feed z `data.odt.org.pl` (aplikacje,
  wyszukiwarki połączeń, badacze). Nikt nie czyta logów; o błędzie dowiadują się wtedy,
  gdy kurs znika z wyszukiwarki.
- **Cel:** opublikowany feed zawsze obejmuje dzisiejszy dzień, sięga możliwie daleko
  w przód i jest poprawny. Lepiej zostawić wczorajszą wersję niż wystawić zepsutą.
- **Powiązane repozytoria:** backend ODT (`jotazet/OtwarteDaneTransportowe-backend`,
  katalog, planer, GTFS-RT) i jego frontend. FeedsODT obsługuje wyłącznie statyczne
  GTFS; realtime zgłasza się w katalogu ODT przy feedzie oryginalnym, a planer łączy go
  z kopią stąd przez `realtime_from`.

## Jak tu pracować

- **Bądź sceptycznym partnerem, nie wykonawcą poleceń.** Sprawdź prośbę względem celu
  i domeny. Jeśli pomysł jest zły, przesadzony albo jest prostsza droga, powiedz to przed
  implementacją. Krótko, z kompromisem.
- **Rób to, o co proszono, nic obok.** Bez poprawek „przy okazji" i bez listy propozycji
  na końcu. Uwaga spoza zakresu to najwyżej jedno zdanie.
- **Pytaj zamiast zgadywać** o zachowanie konkretnego źródła, reguły przewoźnika, kontrakt
  z katalogiem i planerem ODT i niejasny zakres.
- **Żadnych zmyślonych faktów.** Nie wymyślaj pól GTFS, adresów portali, dat publikacji,
  barw linii ani zachowania źródeł. Sprawdź w kodzie, w `config/`, w żywych danych albo
  w specyfikacji; niesprawdzone oznacz jako niesprawdzone. Stosuj skill **`gtfs-feeds`**.
- **Prosto i chirurgicznie.** Stosuj skill **`karpathy-guidelines`**: minimum kodu, które
  rozwiązuje problem, bez spekulatywnych abstrakcji, każda zmieniona linia wynika z prośby.

## Twarde zasady

1. **Język.** Komentarze, docstringi, komunikaty logów, commity, PR-y, `README.md`
   i `docs/` są **po polsku**. Identyfikatory w kodzie po angielsku, jak dotąd. Wyjątek:
   pliki skopiowane z backendu ODT (`.claude/hooks/protect-main.sh` i jego test) zostają
   po angielsku, żeby poprawki dało się przenosić między repozytoriami.
2. **Domena.** Skill **`gtfs-feeds`** przy każdej zmianie źródeł, przebudowy, scalania,
   kształtowania, publikacji i harmonogramu.
3. **Adresy i identyfikatory są kontraktem.** Nazwa pliku wyjściowego, pola `index.json`
   i `trip_id` kopii (po nich GTFS-RT dopasowuje się do kursów) zmieniają się tylko
   świadomie, z ustaleniem z ODT.
4. **Zależności.** Najpierw biblioteka standardowa albo to, co już jest
   (`requests`, `beautifulsoup4`, `APScheduler`). Nową zależność uzasadnij w PR,
   dopisz do `pyproject.toml` i `requirements.txt`.
5. **Dokumentacja jest częścią zmiany.** Nowe źródło, zmienna, polecenie albo krok
   wdrożenia trafia do `README.md` lub `docs/wdrozenie.md` na tej samej gałęzi.
6. **Git.** Według sekcji niżej. Pilnuje tego `.claude/hooks/protect-main.sh` (tylko
   polecenia, które rozpoznaje; to nie gwarancja).

## Przepływ git

- **Nigdy commit ani push na `main`.** Każda zmiana idzie przez własną gałąź i pull request.
- Gałęzie: `<typ>/<krótki-opis-kebab>`. Typy: `feature`, `fix`, `perf`, `refactor`,
  `docs`, `chore`, `test`. Przykład: `fix/railrouting-fill-gaps-loop`.
- Commity jak dotąd w tym repozytorium: temat po polsku w trybie rozkazującym,
  najwyżej 72 znaki, bez prefiksu typu (np. `Uruchamiaj silniki routujące tylko w nocnym
  oknie kształtowania`), potem treść, która wyjaśnia **dlaczego**: ograniczenie, które
  ukształtowało kod, co odrzucono, co wyszło na żywych danych.
- Commituj po każdym spójnym, sprawdzonym etapie, bez czekania na prośbę.
- **Bez atrybucji AI:** żadnych `Co-Authored-By` i „Generated with" w commitach i PR-ach.
  `.claude/settings.json` wyłącza je w Claude Code.
- Push i PR tylko na prośbę użytkownika.
- **Wydania:** tag `vX.Y.Z` na `main` wdraża się sam na produkcję przez CI
  (`docs/wdrozenie.md`). Tagi i wydania tworzysz tylko na prośbę użytkownika; hook pyta
  przed każdym pushem taga `v*`.

## Architektura w skrócie

- **Jeden demon** (`python -m feedsodt daemon`, APScheduler): każde źródło ma swój slot
  (`run_at` w `config/sources.toml`), przebiegi idą po kolei pod blokadą plikową
  `gtfs/.work/.lock`.
- **Przepływ źródła:** resolver (`feedsodt/sources/`: `direct`, `ckan`, `html`,
  `nextcloud`) -> pobranie -> wybór paczek (`select`: `single` albo `today_forward`, scalanie w `merge.py`) ->
  przebudowa (`feedsodt/transform/`: `odt`, `ska`) -> kształtowanie (`feedsodt/shaping/`,
  silniki GraphHopper i OpenRailRouting) -> walidacja -> atomowa publikacja + `index.json`
  (`publish.py`).
- **Silniki routujące** działają tylko w nocnym oknie (`shaper/engines.crontab`,
  00:45-07:30); `tests/test_engines_window.py` pilnuje, żeby sloty z `shape` się w nim
  mieściły.
- **nginx** (`nginx/default.conf`) serwuje `gtfs/` tylko do odczytu; to jedyna usługa
  widoczna z zewnątrz.

## Mapa repozytorium

```
feedsodt/                 kod: runner, config, merge, publish, sources/, transform/, shaping/
config/sources.toml       źródła, ich sloty i przebudowa
config/colors/            tabele barw linii
shaper/                   silniki routujące: konfiguracja, łatki, okno nocne
nginx/                    serwowanie feedów
scripts/deploy/           wydania: feedsodt-deploy, release.sh, envfile.py
tests/                    pytest, bez sieci (sztuczne paczki GTFS w tmp_path)
docs/wdrozenie.md         serwer, wydania, zmienne na GitHubie
.claude/                  ustawienia, hooki i skille projektu
```

## Polecenia

```sh
docker compose up -d --build                                   # lokalnie, http://localhost:8090/gtfs/
docker compose exec fetcher python -m feedsodt run-once --only mld
docker compose exec fetcher python -m feedsodt list
uv venv --python 3.13 && uv pip install -e ".[dev]"            # środowisko testów
.venv/bin/ruff check . && .venv/bin/python -m pytest           # to samo, co CI
```

## Definicja ukończenia

- `ruff check` i `pytest` przechodzą; CI uruchamia je w każdym PR.
- Nowa logika ma testy, bez sieci. Zmiana przebudowy albo scalania jest sprawdzona też
  na żywym feedzie (`run-once --only <id>`), z porównaniem przed i po.
- `README.md` lub `docs/wdrozenie.md` odpowiadają zmianie.
- Przed otwarciem PR: `/simplify`, potem `/code-review`. Popraw to, co prawdziwe,
  i napisz, co pominięto.
