---
name: gtfs-feeds
description: Domena FeedsODT - źródła GTFS i ich resolvery, wybór i scalanie paczek, przebudowa (barwy, perony), geometria przejazdu, walidacja, publikacja pod stałym adresem, index.json, harmonogram slotów i nocne okno silników, zgodność z GTFS-RT i planerem ODT. Używaj przy każdej zmianie config/sources.toml, feedsodt/ (sources, merge, transform, shaping, publish, runner) i przy każdym twierdzeniu o tym, jak działa GTFS, konkretne źródło albo katalog i planer ODT.
---

# Feedy GTFS w FeedsODT

FeedsODT jest pośrednikiem: przewoźnik publikuje plik, my publikujemy jego kopię pod stałym
adresem, a planer ODT i inni odbiorcy pobierają ją co noc. Błąd domenowy jest tu najdroższy,
bo jest cichy: zła paczka, zgubiony dzień albo zmieniony `trip_id` nie wywraca niczego,
tylko kursy znikają z wyszukiwarek albo tracą pozycje na żywo.

## Źródła prawdy (przeczytaj, zanim zaczniesz)

| Potrzeba | Gdzie |
| --- | --- |
| Pola i pliki GTFS | [GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/), [best practices](https://gtfs.org/documentation/schedule/schedule-best-practices/) |
| Dopasowanie realtime do kursów | [GTFS Realtime reference](https://gtfs.org/documentation/realtime/reference/) (`TripDescriptor.trip_id`) |
| Jak działa dane źródło | jego blok w `config/sources.toml` z komentarzem, potem żywy portal |
| Gwarancje publikacji, polityki, dodanie miasta | `README.md` |
| Kształtowanie i silniki | `shaper/README.md`, `feedsodt/shaping/` |
| Wdrożenie i harmonogram na serwerze | `docs/wdrozenie.md`, `shaper/engines.crontab` |
| Katalog i planer ODT | repozytorium `jotazet/OtwarteDaneTransportowe-backend` (`planner/`, `docs/domain/`) |

## Przepływ pracy

1. **Zobacz żywe dane.** Zanim zmienisz resolver albo przebudowę, pobierz prawdziwy plik
   i sprawdź, co w nim jest. Twierdzenie o źródle bez sprawdzenia oznacz jako niesprawdzone.
2. **Sprawdź specyfikację** pola, które ruszasz. Jeśli GTFS czegoś nie wyraża, powiedz to
   i zdecyduj jawnie.
3. **Sprawdź niezmienniki niżej.** Zmiana, która łamie któryś, wymaga zgody użytkownika,
   nie cichego wyjątku.
4. **Pomyśl o odbiorcach:** kto pobiera ten plik i co się u niego zepsuje, gdy zmieni się
   adres, identyfikator albo zakres dat.
5. **Pytaj, gdy nie wiesz.** Nie zmyślaj pól, adresów portali, dat publikacji ani zachowania
   przewoźnika.

## Niezmienniki

- **Adres jest stały.** `output` źródła i wpis w `index.json` się nie zmieniają; planer
  i aplikacje pobierają je latami. Usunięcie źródła nie kasuje jego pliku na serwerze, więc
  sprzątnięcie to osobny, jawny krok.
- **Opublikowany feed obejmuje dzisiaj.** `index.json` mówi to w `covers_today`
  i `days_ahead`. Paczki doklejamy tylko wtedy, gdy następna zaczyna się dokładnie dzień po
  końcu poprzedniej: zakładka dubluje kursy, przerwa zostawia dziurę.
- **Błąd nie kasuje danych.** Nieudane pobranie, walidacja albo kształtowanie zostawia
  poprzednią wersję. Awaria jednego źródła nie dotyka pozostałych.
- **Publikacja jest atomowa i deterministyczna.** `os.replace` w obrębie jednego systemu
  plików; to samo wejście daje bajt w bajt ten sam plik, więc brak zmian = brak zapisu
  i odbiorca dostaje `304`.
- **`trip_id` kopii = `trip_id` oryginału**, chyba że scalanie musi je rozdzielić (prefiks
  paczki w `merge.py`). GTFS-RT jest zgłaszany przy feedzie oryginalnym i dopasowuje się po
  `trip_id`, więc przebudowa, która je zmienia, gasi pozycje na żywo w planerze.
- **Jeden przebieg naraz.** Blokada `gtfs/.work/.lock`; slot, który na nią trafi, czeka
  co 2 minuty, najwyżej godzinę.
- **Kształtowanie mieści się w nocnym oknie silników** (00:45-07:30 z ponowieniami);
  pilnuje tego `tests/test_engines_window.py`.
- **Silniki routujące nie są publiczne:** porty tylko na `127.0.0.1`.

## Przypadki do testów

- Portal, który nie odpowiada, zwraca błąd HTTP, przekierowuje albo podmienia plik pod tym
  samym adresem bez daty i rozmiaru.
- Uszkodzony ZIP, brak wymaganej tabeli, zmiana schematu między paczkami.
- Paczki zachodzące na siebie, z przerwą, w losowej kolejności, z archiwalną na końcu.
- Feed, który kończy się jutro; feed, który zaczyna się jutro.
- Daty przy zmianie czasu (koniec marca i października) i na przełomie miesiąca.
- Kursy poza obszarem kształtowania i stacje po dwóch stronach granicy.
