## Co

<!-- Jedno-dwa zdania: co się zmienia dla odbiorcy feedów albo dla działania usługi. -->

## Dlaczego

<!-- Problem i kogo dotyczy (planer ODT, odbiorcy data.odt.org.pl, źródło). Link do issue, jeśli jest. -->

## Jak sprawdzone

<!-- Testy, `run-once --only <id>` na żywym feedzie (przed i po), index.json, walidacja. -->

## Lista kontrolna

- [ ] `ruff check` i `pytest` przechodzą
- [ ] Adresy, `index.json` i `trip_id` kopii bez zmian albo zmiana uzgodniona z ODT
- [ ] `README.md` lub `docs/wdrozenie.md` zaktualizowane albo niepotrzebne
- [ ] Nowa zmienna: w `.env.example`, w `docker-compose.yml` i w `RELEASE_VARIABLES` (`scripts/deploy/envfile.py`) albo w pliku hosta
- [ ] `/simplify` i `/code-review` uruchomione; pozostałe uwagi wyjaśnione niżej

## Uwagi dla recenzenta

<!-- Kompromisy, kroki wdrożenia (zmienne na GitHubie, plik hosta), dalsze kroki. -->
