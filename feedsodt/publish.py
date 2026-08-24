"""Publikacja pliku pod stałą nazwą i utrzymanie index.json.

Dwie zasady:

* podmiana jest atomowa (``os.replace``), więc serwer WWW nigdy nie odda połowy
  pliku komuś, kto akurat trafił w moment aktualizacji;
* przy jakimkolwiek błędzie poprzednia wersja zostaje na miejscu — wczorajszy
  działający feed jest lepszy niż dzisiejszy uszkodzony.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"


def _write_atomic(path: Path, payload: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


class Index:
    """Metadane opublikowanych feedów — również dla konsumentów, nie tylko dla nas."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, output_dir: Path) -> Index:
        path = output_dir / INDEX_NAME
        data: dict[str, Any] = {"feeds": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("feeds"), dict):
                    data = loaded
            except (OSError, json.JSONDecodeError) as exc:
                # Uszkodzony index to nie powód, żeby nie publikować — najwyżej
                # pierwszy przebieg pobierze wszystko od nowa.
                log.warning("%s nieczytelny (%s), zaczynam od pustego", path, exc)
        return cls(path, data)

    def entry(self, name: str) -> dict[str, Any]:
        return self.data["feeds"].get(name, {})

    def put(self, name: str, entry: dict[str, Any]) -> None:
        self.data["feeds"][name] = entry

    def save(self, now: datetime) -> None:
        self.data["generated_at"] = now.isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self.path, json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )


def publish(staged: Path, output_dir: Path, output_name: str) -> Path:
    """Przenosi gotowy plik pod docelową nazwę jedną niepodzielną operacją."""
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / output_name

    # os.replace jest atomowe tylko w obrębie jednego systemu plików, dlatego
    # katalog roboczy domyślnie leży wewnątrz katalogu wyjściowego.
    if staged.stat().st_dev != output_dir.stat().st_dev:
        raise OSError(
            f"katalog roboczy ({staged.parent}) jest na innym systemie plików niż "
            f"{output_dir} — atomowa podmiana niemożliwa"
        )

    os.replace(staged, dest)
    dest.chmod(0o644)
    return dest
