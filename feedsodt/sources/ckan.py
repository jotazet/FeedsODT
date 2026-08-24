"""Resolver dla portali opartych o CKAN (m.in. otwartedane.metropoliagzm.pl).

CKAN wystawia ustandaryzowane API, więc ten jeden moduł obsługuje GZM i każdy inny
portal tego typu — bez linijki kodu specyficznej dla konkretnego miasta.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from ..config import Candidate, SourceConfig
from ..http import TIMEOUT
from . import ResolveError, resolver

log = logging.getLogger(__name__)


@resolver("ckan")
def resolve_ckan(cfg: SourceConfig, session: requests.Session) -> list[Candidate]:
    base_url = cfg.option("base_url").rstrip("/")
    dataset = cfg.option("dataset")
    wanted_format = str(cfg.options.get("format", "ZIP")).upper()
    name_pattern = cfg.options.get("name_pattern")

    endpoint = f"{base_url}/api/3/action/package_show"
    try:
        response = session.get(endpoint, params={"id": dataset}, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ResolveError(f"{endpoint}: {exc}") from exc
    except ValueError as exc:
        raise ResolveError(f"{endpoint}: odpowiedź nie jest poprawnym JSON-em") from exc

    if not payload.get("success"):
        raise ResolveError(f"{endpoint}: CKAN zwrócił success=false dla zbioru {dataset!r}")

    resources = payload.get("result", {}).get("resources") or []
    matcher = re.compile(str(name_pattern)) if name_pattern else None
    candidates: list[Candidate] = []

    for resource in resources:
        if str(resource.get("format") or "").upper() != wanted_format:
            continue
        url = resource.get("url")
        if not url:
            continue
        name = str(resource.get("name") or "")
        if matcher and not matcher.search(name):
            continue

        # Nazwa zasobu bywa pusta — wtedy ostatni segment URL-a jest lepszy niż nic.
        fallback = Path(unquote(urlparse(url).path)).name
        # Daty publikacji z CKAN świadomie nie bierzemy: mówi, kiedy zasób wgrano,
        # a nie od kiedy obowiązuje. Okno i tak czytamy z feed_info.txt po pobraniu.
        candidates.append(Candidate(url=url, filename=name or fallback))

    if not candidates:
        raise ResolveError(
            f"{endpoint}: zbiór {dataset!r} nie ma żadnego zasobu w formacie {wanted_format}"
        )

    log.info("[%s] CKAN: znaleziono %d zasobów %s", cfg.name, len(candidates), wanted_format)
    return candidates
