"""Rejestr resolverów.

Resolver odpowiada na jedno pytanie: „co jest u tego źródła do wzięcia?".
Nie pobiera plików, nic nie zapisuje i nie decyduje, który plik jest najnowszy —
tym zajmuje się wspólny rdzeń. Dzięki temu nowe źródło to jedna funkcja, a nie
kopia całego skryptu.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable

import requests

from ..config import Candidate, ConfigError, SourceConfig

Resolver = Callable[[SourceConfig, requests.Session], list[Candidate]]

RESOLVERS: dict[str, Resolver] = {}

_loaded = False


class ResolveError(Exception):
    """Nie udało się ustalić listy kandydatów u źródła."""


def resolver(name: str) -> Callable[[Resolver], Resolver]:
    def register(func: Resolver) -> Resolver:
        if name in RESOLVERS:
            raise RuntimeError(f"resolver {name!r} zarejestrowany dwukrotnie")
        RESOLVERS[name] = func
        return func

    return register


def load_resolvers() -> None:
    """Importuje wszystkie moduły z tego pakietu, żeby dekoratory się wykonały."""
    global _loaded
    if _loaded:
        return
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
    _loaded = True


def get_resolver(name: str) -> Resolver:
    load_resolvers()
    try:
        return RESOLVERS[name]
    except KeyError:
        raise ConfigError(
            f"nieznany resolver {name!r}; dostępne: {', '.join(sorted(RESOLVERS))}"
        ) from None
