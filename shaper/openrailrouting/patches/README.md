# Łatki na OpenRailRouting

Dockerfile klonuje tag `ORR_VERSION` z
https://github.com/geofabrik/OpenRailRouting i nakłada wszystkie `*.patch`
z tego katalogu (`git apply`, w kolejności nazw) przed `mvn package`. Jeśli
łatka przestanie się nakładać — np. po podbiciu `ORR_VERSION` — build się
wywraca, zamiast po cichu ją pominąć. Wtedy sprawdź, czy upstream naprawił
błąd sam; jeśli tak, usuń łatkę.

## 0001-fix-fill-gaps-infinite-loop.patch

`MatchResource` (`POST /match?fill_gaps=true`), dotyczy v1.1 i v1.2 / master
z 2026-09-30. Dwa błędy w pętli, która na przemian dopasowuje ślad i przerzuca
routing przez dziury:

1. **Nieskończona pętla.** Dziurę routowano tylko przy
   `mapMatching.matchingAttempted()`, czyli `processedUpTo > 0`. Gdy
   dopasowanie urywało się między PIERWSZYM a drugim punktem, `processedUpTo`
   zostawało 0, dziury nikt nie routował, a to samo dopasowanie od offsetu 0
   wykonywało się bez końca. Klient po swoim timeoucie zrywał połączenie, ale
   Jetty nie przerywa wątku — każde takie żądanie zajmowało rdzeń do restartu
   kontenera. Na produkcji siedem takich wątków trzymało ~5 z 6 rdzeni przez
   sześć tygodni.
2. **Zły offset po drugiej dziurze.** `getProcessedPointsCount()` liczy od
   początku śladu, a pętla dodawała go do offsetu zamiast przypisać. Po
   dziurze w środku śladu offset przeskakiwał punkty albo wychodził poza
   listę (`IndexOutOfBoundsException` → błąd 500 i fallback na `/route`).

Po łatce każdy obrót pętli przesuwa offset co najmniej o jeden punkt, więc
żądanie zawsze się kończy.
