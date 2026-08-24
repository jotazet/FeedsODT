"""Wspólna warstwa HTTP: jedna sesja z retry, sensownymi timeoutami i pobieraniem
strumieniowym. Resolvery i pipeline nie tworzą własnych połączeń."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"FeedsODT/{__version__} (+https://github.com/; GTFS mirror)"

#: (nawiązanie połączenia, oczekiwanie na kolejny fragment)
TIMEOUT = (10, 60)

CHUNK = 1024 * 256

#: Bezpiecznik na wypadek, gdyby źródło zaczęło serwować coś nieoczekiwanie wielkiego.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


class DownloadError(Exception):
    """Pobranie się nie udało — wołający ma zostawić stary plik w spokoju."""


@dataclass(frozen=True)
class DownloadInfo:
    """Co wiadomo o pobranym pliku.

    Świadomie nie ma tu rozmiaru ani skrótu: o publikacji decyduje sha256
    pliku *po* przebudowie i kształtowaniu (``sha256_file`` w runnerze), a nie
    tego, co przyszło z sieci. Liczenie skrótu w trakcie pobierania oznaczałoby
    przemielenie każdego bajtu bez powodu.
    """

    path: Path
    #: Nazwa z Content-Disposition albo — gdy serwer jej nie poda — ta zgadnięta
    #: przez resolvera z adresu.
    filename: str


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def filename_from_response(response: requests.Response, fallback: str) -> str:
    """Prawdziwa nazwa pliku siedzi w Content-Disposition — URL bywa nieinformatywny
    (np. /hdb/download/127/)."""
    disposition = response.headers.get("Content-Disposition", "")
    match = _FILENAME_RE.search(disposition)
    if match:
        return Path(match.group(1).strip()).name
    return fallback


def download_to(
    session: requests.Session,
    url: str,
    dest: Path,
    fallback_name: str,
    auth: tuple[str, str] | None = None,
) -> DownloadInfo:
    """Pobiera URL do `dest`. Rzuca DownloadError zamiast zostawiać obcięty plik."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    try:
        with session.get(
            url, stream=True, timeout=TIMEOUT, allow_redirects=True, auth=auth
        ) as response:
            response.raise_for_status()
            filename = filename_from_response(response, fallback_name)

            with dest.open("wb") as handle:
                for chunk in response.iter_content(CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise DownloadError(
                            f"{url}: przekroczono limit {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB"
                        )
                    handle.write(chunk)

            declared = response.headers.get("Content-Length")
    except requests.RequestException as exc:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"{url}: {exc}") from exc
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise

    # Ucięte pobranie potrafi zwrócić 200 i połowę pliku — tu się to wyłapuje.
    if declared is not None and declared.isdigit() and int(declared) != written:
        dest.unlink(missing_ok=True)
        raise DownloadError(f"{url}: pobrano {written} B, a serwer zapowiadał {declared} B")

    log.debug("pobrano %s (%.1f MB) -> %s", url, written / 1e6, dest.name)
    return DownloadInfo(path=dest, filename=filename)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
