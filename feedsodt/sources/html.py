"""Resolver dla zwykłych stron z linkiem do pliku GTFS.

Jeden moduł obsługuje wszystkie miasta, które po prostu wystawiają odnośnik na
stronie — różnią się tylko tym, po czym ten odnośnik rozpoznać. Dlatego zamiast
kodu na miasto są trzy opcje w sources.toml:

``link_pattern``
    Wyrażenie regularne na adresie odnośnika. Wystarczy, gdy w URL-u jest słowo
    „gtfs" (Gorzów, Rybnik, Wrocław).

``link_text``
    Wyrażenie regularne na tekście odnośnika. Konieczne, gdy nazwa pliku jest
    nieczytelna — Zielona Góra serwuje ``dla_deweloperow_74ib2.zip`` i tylko
    podpis „Rozkład jazdy w formacie GTFS" mówi, że to właściwy plik.

``hint_date``
    Wyrażenie regularne wyłuskujące datę początku obowiązywania z tekstu wiersza
    lub z adresu. Nie jest potrzebne, gdy plik jest jeden. Przy 69 wersjach
    Wrocławia pozwala pobrać dwie zamiast wszystkich.

Domyślnie, gdy nie podano żadnego wzorca, brane są odnośniki kończące się na .zip.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import Candidate, SourceConfig
from ..http import TIMEOUT
from . import ResolveError, resolver

log = logging.getLogger(__name__)

#: Kolejność pól w hint_date. Prawie wszyscy używają DDMMYYYY (Wrocław:
#: OtwartyWroclaw_..._22082026), ale Nextcloud Częstochowy YYYY.MM.DD.
_ORDERS = {"dmy": (2, 1, 0), "ymd": (0, 1, 2)}


def parse_hint(text: str, pattern: str, order: str) -> date | None:
    """Wyłuskuje datę z tekstu. Zwraca None, gdy nie pasuje — podpowiedź jest
    opcjonalna i jej brak nigdy nie może wywrócić przebiegu."""
    match = re.search(pattern, text)
    if not match or len(match.groups()) != 3:
        return None
    try:
        year, month, day = (int(match.group(1 + index)) for index in _ORDERS[order])
        return date(year, month, day)
    except (ValueError, KeyError):
        return None


#: Kolejność ma znaczenie: w tabeli Wrocławia nazwa pliku stoi w innej komórce
#: niż przycisk, więc potrzebny jest cały <tr>, a nie otaczający <td>.
#: find_parent z listą zwróciłby najbliższy pasujący element, czyli właśnie <td>.
_ROW_TAGS = ("tr", "li", "p", "td")


def _row_text(anchor) -> str:
    """Tekst kontenera, w którym stoi odnośnik — stamtąd bierzemy podpowiedź daty."""
    for tag in _ROW_TAGS:
        parent = anchor.find_parent(tag)
        if parent is not None:
            return " ".join(parent.get_text(" ", strip=True).split())
    return " ".join(anchor.get_text(" ", strip=True).split())


@resolver("html")
def resolve_html(cfg: SourceConfig, session: requests.Session) -> list[Candidate]:
    page_url = cfg.option("url")
    link_pattern = cfg.options.get("link_pattern")
    link_text = cfg.options.get("link_text")
    hint_date = cfg.options.get("hint_date")
    hint_order = str(cfg.options.get("hint_order", "dmy"))

    try:
        response = session.get(page_url, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ResolveError(f"{page_url}: {exc}") from exc

    # html.parser zamiast lxml — strony mają po kilkadziesiąt kB, więc nie ma
    # po co ciągnąć kompilowanej zależności do obrazu.
    soup = BeautifulSoup(response.text, "html.parser")
    href_re = re.compile(str(link_pattern), re.IGNORECASE) if link_pattern else None
    text_re = re.compile(str(link_text), re.IGNORECASE) if link_text else None

    candidates: list[Candidate] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = urljoin(page_url, anchor["href"])
        text = anchor.get_text(" ", strip=True)

        if href_re and not href_re.search(url):
            continue
        if text_re and not text_re.search(text):
            continue
        if not href_re and not text_re and not url.lower().split("?")[0].endswith(".zip"):
            continue
        if url in seen:
            continue
        seen.add(url)

        row = _row_text(anchor)
        name = Path(unquote(urlparse(url).path)).name or f"{cfg.name}.zip"
        candidates.append(
            Candidate(
                url=url,
                filename=name,
                hint_start=parse_hint(f"{row} {url}", str(hint_date), hint_order)
                if hint_date
                else None,
            )
        )

    if not candidates:
        raise ResolveError(
            f"{page_url}: żaden odnośnik nie pasuje do wzorca "
            f"(link_pattern={link_pattern!r}, link_text={link_text!r}) — "
            "prawdopodobnie zmienił się układ strony"
        )

    log.info("[%s] %s: znaleziono %d odnośników", cfg.name, urlparse(page_url).netloc, len(candidates))
    return candidates
