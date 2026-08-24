"""Resolver dla publicznych udostępnień Nextcloud/ownCloud.

MPK Częstochowa nie ma portalu danych — wystawia chmurę z katalogiem na każdą
publikację (``2026.08.21/``), a w środku kilka formatów (GTFS, DBF, Eksport,
TransXChange). Strona udostępnienia to aplikacja JavaScriptowa, więc nie da się
jej sparsować jak HTML-a; za to Nextcloud wystawia WebDAV, w którym token
udostępnienia jest loginem, a hasło pozostaje puste.

Bierzemy najnowszy katalog i w nim plik pasujący do wzorca.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

import requests

from ..config import Candidate, SourceConfig
from ..http import TIMEOUT
from . import ResolveError, resolver
from .html import parse_hint

log = logging.getLogger(__name__)

_HREF_RE = re.compile(r"<[a-zA-Z]+:href>([^<]*)</[a-zA-Z]+:href>")

_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
)


def _propfind(session: requests.Session, url: str, token: str) -> list[str]:
    """Zwraca ścieżki bezpośrednich dzieci katalogu, bez niego samego."""
    try:
        response = session.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
            auth=(token, ""),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ResolveError(f"{url}: {exc}") from exc

    own = unquote(urlparse(url).path).rstrip("/")
    return [
        path
        for path in (unquote(href) for href in _HREF_RE.findall(response.text))
        if path.rstrip("/") != own
    ]


@resolver("nextcloud")
def resolve_nextcloud(cfg: SourceConfig, session: requests.Session) -> list[Candidate]:
    base_url = cfg.option("base_url").rstrip("/")
    token = cfg.option("token")
    folder = str(cfg.options.get("folder", "")).strip("/")
    file_pattern = cfg.option("file_pattern")
    hint_date = cfg.options.get("hint_date")
    hint_order = str(cfg.options.get("hint_order", "ymd"))

    parts = urlparse(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"

    root = f"{base_url}/public.php/webdav/"
    if folder:
        root += quote(folder) + "/"

    folders = sorted(path for path in _propfind(session, root, token) if path.endswith("/"))
    if not folders:
        raise ResolveError(f"{root}: brak podkatalogów z publikacjami")

    # Katalogi nazwane datą sortują się leksykograficznie zgodnie z chronologią.
    newest = folders[-1]
    log.info("[%s] najnowsza publikacja: %s", cfg.name, PurePosixPath(newest.rstrip("/")).name)

    matcher = re.compile(str(file_pattern), re.IGNORECASE)
    entries = _propfind(session, origin + quote(newest), token)
    candidates = []

    for path in entries:
        if path.endswith("/"):
            continue
        name = PurePosixPath(path).name
        if not matcher.search(name):
            continue
        candidates.append(
            Candidate(
                url=origin + quote(path),
                filename=name,
                hint_start=parse_hint(name, str(hint_date), hint_order) if hint_date else None,
                auth=(token, ""),
            )
        )

    if not candidates:
        available = ", ".join(PurePosixPath(p).name for p in entries if not p.endswith("/"))
        raise ResolveError(
            f"{newest}: brak pliku pasującego do wzorca {file_pattern!r} (jest: {available})"
        )
    return candidates
