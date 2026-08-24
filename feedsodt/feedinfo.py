"""``feed_info.txt`` wystawcy feedu.

Publikujemy cudze rozkłady pod własnymi, stałymi adresami, więc wystawcą
opublikowanej paczki jest Otwarte Dane Transportowe, a nie przewoźnik —
u niego zostaje ``agency.txt``. Bez tego konsument, który zgłasza błąd
w danych, pisze do R&G PLUS albo do kiedyPrzyjedzie.pl zamiast do nas.

Oryginały podają tu co popadnie: „kiedyPrzyjedzie.pl" (Olkusz, ZKKM),
„R&G PLUS Sp. z o.o." (MLD), a POLREGIO nie ma ``feed_info.txt`` w ogóle.
"""

from __future__ import annotations

import csv
import io
import zipfile

PUBLISHER_NAME = "Otwarte Dane Transportowe"
PUBLISHER_URL = "https://odt.org.pl/"
CONTACT_EMAIL = "kontakt@odt.org.pl"
LANG = "pl"

COLUMNS = (
    "feed_publisher_name",
    "feed_publisher_url",
    "feed_lang",
    "feed_start_date",
    "feed_end_date",
    "feed_version",
    "feed_contact_email",
    "feed_contact_url",
)


def write(
    out: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    window: tuple[str, str],
    version: str,
) -> None:
    """Dopisuje feed_info.txt z ODT jako wystawcą i podanym oknem (YYYYMMDD)."""
    with out.open(entry, "w") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerow(
            [
                PUBLISHER_NAME,
                PUBLISHER_URL,
                LANG,
                window[0],
                window[1],
                version,
                CONTACT_EMAIL,
                PUBLISHER_URL,
            ]
        )
        stream.flush()
        stream.detach()
