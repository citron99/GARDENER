"""Stable taxonomy helpers for catalog recommendation matching."""

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

_COUNTRY_ALIASES = {
    "latvia": "LV",
    "latvija": "LV",
    "латвия": "LV",
    "riga": "LV",
    "rīga": "LV",
    "рига": "LV",
    "lithuania": "LT",
    "lietuva": "LT",
    "литва": "LT",
    "vilnius": "LT",
    "vilnius city": "LT",
    "estonia": "EE",
    "eesti": "EE",
    "эстония": "EE",
    "tallinn": "EE",
}


def country_code_from_region(value: str | None) -> str | None:
    """Return a conservative ISO 3166-1 alpha-2 code without guessing unknown places."""
    if not value:
        return None
    normalized = value.strip().casefold()
    if re.fullmatch(r"[a-z]{2}", normalized):
        return normalized.upper()
    if normalized in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[normalized]
    parts = [part.strip() for part in re.split(r"[,;/]", normalized) if part.strip()]
    for part in reversed(parts):
        if part in _COUNTRY_ALIASES:
            return _COUNTRY_ALIASES[part]
    return None


def normalize_alias(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolve_catalog_code(
    db: Session,
    alias_type: str,
    value: str | None,
    locale: str | None = None,
) -> str | None:
    """Resolve a localized label to a stable code, preferring locale-specific aliases."""
    if not value:
        return None
    raw = value.strip()
    if alias_type == "country" and re.fullmatch(r"[A-Za-z]{2}", raw):
        return raw.upper()
    if alias_type != "country" and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{1,99}", raw):
        return raw.lower()

    from app.models import CatalogTaxonomyAlias

    normalized = normalize_alias(raw)
    locales = [locale, "*"] if locale in {"ru", "lv", "en"} else ["*"]
    for selected_locale in locales:
        code = db.scalar(select(CatalogTaxonomyAlias.stable_code).where(
            CatalogTaxonomyAlias.alias_type == alias_type,
            CatalogTaxonomyAlias.locale == selected_locale,
            CatalogTaxonomyAlias.normalized_alias == normalized,
            CatalogTaxonomyAlias.active.is_(True),
        ))
        if code:
            return code
    if alias_type == "country":
        return country_code_from_region(raw)
    return None
