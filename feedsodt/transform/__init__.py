"""Rejestr transformacji feedu.

Niektórzy przewoźnicy publikują dane poprawne składniowo, ale bezużyteczne
w praktyce — bez nazw tras, bez kierunku kursu, z jedną trasą na numer pociągu.
Transformacja to jedna funkcja `(źródło, cel, kontekst) -> None`, która
przepisuje taki feed na sensowny, uruchamiana po pobraniu i przed walidacją.

Kontrakt jest celowo taki sam jak u resolverów: dekorator rejestrujący i wybór
po nazwie z sources.toml. Źródło bez pola `transform` nie jest niczym ruszane.

`Context` niesie to, czego transformacja nie ma skąd wziąć sama: sesję HTTP
(gdy potrzebuje danych z zewnątrz, jak lista peronów PLK), katalog na dane
trzymane między przebiegami, katalog konfiguracji i pozostałe pola bloku
`[[source]]`. Wszystko jest opcjonalne, więc transformację da się wywołać
w teście jednym argumentem mniej.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import ConfigError

Transform = Callable[..., Any]

TRANSFORMS: dict[str, Transform] = {}

_loaded = False


class TransformError(Exception):
    """Feedu nie da się bezpiecznie przepisać — lepiej zostawić poprzednią wersję."""


@dataclass(frozen=True)
class Context:
    """Zasoby wspólne, udostępniane transformacji przez runnera."""

    #: Sesja HTTP z retry i User-Agentem projektu. `None` w testach — wtedy
    #: transformacja musi sobie poradzić bez sieci.
    session: Any = None
    #: Katalog na dane odświeżane między przebiegami (np. lista peronów).
    cache_dir: Path | None = None
    #: Katalog z sources.toml — obok niego leżą tabele kolorów.
    config_dir: Path | None = None
    #: Pozostałe pola bloku [[source]], interpretowane przez transformację
    #: tak samo, jak resolver interpretuje swoje.
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def platforms_cache(self) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / "platforms-list.json"

    @property
    def colors_dir(self) -> Path:
        base = self.config_dir or Path(__file__).resolve().parent.parent.parent / "config"
        return base / "colors"


def transform(name: str) -> Callable[[Transform], Transform]:
    def register(func: Transform) -> Transform:
        if name in TRANSFORMS:
            raise RuntimeError(f"transformacja {name!r} zarejestrowana dwukrotnie")
        TRANSFORMS[name] = func
        return func

    return register


def load_transforms() -> None:
    """Importuje moduły z tego pakietu, żeby dekoratory się wykonały."""
    global _loaded
    if _loaded:
        return
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
    _loaded = True


def get_transform(name: str) -> Transform:
    load_transforms()
    try:
        return TRANSFORMS[name]
    except KeyError:
        raise ConfigError(
            f"nieznana transformacja {name!r}; dostępne: {', '.join(sorted(TRANSFORMS))}"
        ) from None
