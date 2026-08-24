"""Kolory tras: wspólne dla wszystkich transformacji.

Feedy przewoźników przychodzą z pustym ``route_color`` — 28 z 28 tras
w Olkuszu, 70 z 70 w MLD, 26 z 26 w ZKKM. Planer rysuje je wtedy jedną
domyślną barwą i pasażer nie odróżnia linii od linii.

Kolor tekstu liczymy z jasności tła, zamiast kazać go podawać obok. Dzięki
temu zmiana barwy linii to jeden wpis, a nie dwa, i nie da się zostawić
białego napisu na żółtym tle.
"""

from __future__ import annotations


class ColorError(Exception):
    """To nie jest kolor w zapisie szesnastkowym."""


def normalize(value: str) -> str:
    """``"#0462a2"``, ``"0462A2"`` -> ``"0462A2"``. GTFS chce sześciu znaków
    bez kratki, a ludzie wklejają je w każdej postaci."""
    text = value.strip().lstrip("#").upper()
    if len(text) != 6:
        raise ColorError(f"kolor {value!r}: oczekiwano sześciu znaków, jest {len(text)}")
    try:
        int(text, 16)
    except ValueError:
        raise ColorError(f"kolor {value!r}: to nie jest zapis szesnastkowy") from None
    return text


def text_color_for(background: str) -> str:
    """Czarny napis na jasnym tle, biały na ciemnym.

    Liczone automatycznie, żeby podmiana koloru linii nie wymagała pamiętania
    o drugim polu.
    """
    red, green, blue = (int(background[index : index + 2], 16) for index in (0, 2, 4))
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "000000" if luminance > 0.6 else "FFFFFF"
