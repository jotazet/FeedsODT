"""Testy doboru paczek — serce polityki „dzisiaj i jak najdalej w przód".

Tu mieszkają najbardziej podstępne błędy: doklejenie nakładającej się paczki
dubluje kursy, a zbyt ostrożny warunek gubi tydzień rozkładu. Wszystkie
przypadki są odtworzeniem realnych układów z Wrocławia i GZM.
"""

from __future__ import annotations

from datetime import date

from pathlib import Path

from feedsodt.config import Candidate, FeedWindow, Package
from feedsodt.gtfs import KEEP_PAST, KEEP_UNDATED, chain_from_today, shortlist

TODAY = date(2026, 8, 21)


def package(name: str, start: str, end: str) -> Package:
    return Package(
        candidate=Candidate(url=f"https://example.pl/{name}", filename=name),
        window=FeedWindow(date.fromisoformat(start), date.fromisoformat(end)),
        path=Path(f"/tmp/{name}"),
    )


def names(chain: list[Package]) -> list[str]:
    return [package.filename for package in chain]


class TestChainFromToday:
    def test_gzm_sklada_wszystkie_rozlaczne_paczki(self):
        """Realny układ GZM: 9 paczek stykających się co do dnia = 14 dni rozkładu."""
        packages = [
            package("p1", "2026-08-21", "2026-08-21"),
            package("p2", "2026-08-22", "2026-08-22"),
            package("p3", "2026-08-23", "2026-08-23"),
            package("p4", "2026-08-24", "2026-08-27"),
            package("p5", "2026-08-28", "2026-09-10"),
        ]
        chain = chain_from_today(packages, TODAY)

        assert names(chain) == ["p1", "p2", "p3", "p4", "p5"]
        assert chain[-1].window.end == date(2026, 9, 10)

    def test_wroclaw_nie_dokleja_nakladajacej_sie_paczki(self):
        """Kluczowy przypadek. Paczka 127 zaczyna się jutro i sięga dalej, ale
        zachodzi na 125 — doklejenie zdublowałoby kursy w dniach 22.08–07.09."""
        p125 = package("125", "2026-08-17", "2026-09-07")
        p127 = package("127", "2026-08-22", "2026-09-12")

        chain = chain_from_today([p125, p127], TODAY)

        assert names(chain) == ["125"]

    def test_zwraca_pusto_gdy_nic_nie_obejmuje_dzisiaj(self):
        """Wszystko przeterminowane albo jeszcze nieaktualne — wołający ma o tym
        wiedzieć, a nie dostać przypadkowy plik."""
        packages = [
            package("stara", "2026-07-01", "2026-08-20"),
            package("przyszla", "2026-08-22", "2026-09-12"),
        ]

        assert chain_from_today(packages, TODAY) == []

    def test_przerwa_konczy_lancuch(self):
        """Brak paczki na 23.08 — łańcuch kończy się na 22.08 zamiast przeskakiwać
        dziurę i udawać ciągły rozkład."""
        packages = [
            package("p1", "2026-08-21", "2026-08-22"),
            package("p3", "2026-08-24", "2026-08-30"),
        ]

        assert names(chain_from_today(packages, TODAY)) == ["p1"]

    def test_sposrod_obejmujacych_dzisiaj_wygrywa_siegajaca_najdalej(self):
        packages = [
            package("krotka", "2026-08-20", "2026-08-25"),
            package("dluga", "2026-08-19", "2026-09-15"),
        ]

        assert names(chain_from_today(packages, TODAY)) == ["dluga"]

    def test_sposrod_kontynuacji_wygrywa_siegajaca_najdalej(self):
        packages = [
            package("baza", "2026-08-21", "2026-08-21"),
            package("krotka", "2026-08-22", "2026-08-23"),
            package("dluga", "2026-08-22", "2026-08-31"),
        ]

        assert names(chain_from_today(packages, TODAY)) == ["baza", "dluga"]

    def test_kolejnosc_wejscia_nie_ma_znaczenia(self):
        """Tabela Wrocławia nie jest posortowana, więc dobór nie może na tym polegać."""
        packages = [
            package("p3", "2026-08-23", "2026-08-30"),
            package("p1", "2026-08-21", "2026-08-21"),
            package("p2", "2026-08-22", "2026-08-22"),
        ]

        assert names(chain_from_today(packages, TODAY)) == ["p1", "p2", "p3"]

    def test_pojedyncza_paczka_na_caly_okres(self):
        """Zielona Góra i Rybnik: jeden plik na kilka miesięcy."""
        chain = chain_from_today([package("jedna", "2026-03-26", "2026-12-31")], TODAY)

        assert names(chain) == ["jedna"]


class TestShortlist:
    def dated(self, name: str, start: str) -> Candidate:
        return Candidate(
            url=f"https://example.pl/{name}",
            filename=name,
            hint_start=date.fromisoformat(start),
        )

    def undated(self, name: str) -> Candidate:
        return Candidate(url=f"https://example.pl/{name}", filename=name)

    def test_bez_dat_w_nazwach_przechodzi_wszystko(self):
        """GZM: żadna paczka nie ma daty w nazwie, więc o żadnej nie wiadomo nic
        z góry i wszystkie trzeba pobrać."""
        candidates = [self.undated(f"p{i}") for i in range(9)]

        assert shortlist(candidates, TODAY, limit=15) == candidates

    def test_odsiewa_archiwum_gdy_daty_sa(self):
        """Wrocław: 69 plików, z czego istotne są dwa ostatnie sprzed dzisiaj
        i wszystkie przyszłe."""
        candidates = [
            self.dated("stary-1", "2026-01-10"),
            self.dated("stary-2", "2026-02-10"),
            self.dated("przedwczorajszy", "2026-08-08"),
            self.dated("wczorajszy", "2026-08-17"),
            self.dated("jutrzejszy", "2026-08-22"),
        ]

        picked = [c.filename for c in shortlist(candidates, TODAY, limit=15)]

        assert picked == ["wczorajszy", "przedwczorajszy", "jutrzejszy"]
        assert len([c for c in candidates if c.hint_start <= TODAY]) > KEEP_PAST

    def test_zostawia_kilka_bez_daty_jako_zabezpieczenie(self):
        """Gdyby źródło zmieniło konwencję nazw, nowe pliki nie miałyby daty —
        muszą mieć szansę wejść, inaczej mirror zamarłby na starej wersji."""
        candidates = [
            self.dated("z-data", "2026-08-17"),
            *[self.undated(f"bez-daty-{i}") for i in range(5)],
        ]

        picked = shortlist(candidates, TODAY, limit=15)

        assert [c.filename for c in picked if c.hint_start is None] == [
            f"bez-daty-{i}" for i in range(KEEP_UNDATED)
        ]

    def test_respektuje_limit(self):
        candidates = [self.undated(f"p{i}") for i in range(30)]

        assert len(shortlist(candidates, TODAY, limit=15)) == 15
