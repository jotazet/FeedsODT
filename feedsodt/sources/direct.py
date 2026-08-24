"""Resolver dla źródeł o jednym, z góry znanym adresie.

Bywa, że przewoźnik nie ma ani portalu, ani strony z odnośnikiem — po prostu
podaje link do pliku, a plik pod nim się zmienia. Wtedy nie ma czego szukać
i cała robota resolvera sprowadza się do przepisania adresu z konfiguracji.

Wykrycie zmiany i tak opiera się na sha256 pobranej treści, więc stały adres
niczego nie psuje.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from ..config import Candidate, SourceConfig
from . import resolver

log = logging.getLogger(__name__)


@resolver("direct")
def resolve_direct(cfg: SourceConfig, session: requests.Session) -> list[Candidate]:
    url = cfg.option("url")
    name = str(cfg.options.get("filename") or "").strip()
    if not name:
        name = Path(unquote(urlparse(url).path)).name or f"{cfg.name}.zip"

    log.info("[%s] stały adres: %s", cfg.name, url)
    return [Candidate(url=url, filename=name)]
