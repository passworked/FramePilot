from __future__ import annotations

import json
from pathlib import Path
import sys


LANGUAGE_OPTIONS = (
    ("zh", "简体中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
)
SUPPORTED_LANGUAGES = frozenset(code for code, _label in LANGUAGE_OPTIONS)


def resource_path(*parts: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent
    return root.joinpath(*parts)


class Localizer:
    def __init__(
        self,
        english: dict[str, str],
        locale_dir: Path | None = None,
    ) -> None:
        self.catalogs: dict[str, dict[str, str]] = {
            "zh": {source: source for source in english},
            "en": dict(english),
        }
        directory = locale_dir or resource_path("locales")
        for language in ("ja", "ko", "fr", "de", "es"):
            path = directory / f"{language}.json"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                raw = {}
            translated = (
                {
                    str(source): str(value)
                    for source, value in raw.items()
                    if source in english and isinstance(value, str) and value.strip()
                }
                if isinstance(raw, dict)
                else {}
            )
            self.catalogs[language] = {
                source: translated.get(source, english[source])
                for source in english
            }
        self._source_by_translation: dict[str, str] = {}
        for catalog in self.catalogs.values():
            for source, translated in catalog.items():
                self._source_by_translation.setdefault(translated, source)

    def translate(self, text: str, language: str) -> str:
        source = self._source_by_translation.get(text, text)
        return self.catalogs.get(language, self.catalogs["en"]).get(source, source)

    def format(self, text: str, language: str, **values: object) -> str:
        return self.translate(text, language).format(**values)

    def localize_message(self, message: str, language: str) -> str:
        if language == "zh":
            return message
        catalog = self.catalogs.get(language, self.catalogs["en"])
        exact = catalog.get(message)
        if exact is not None:
            return exact
        output = message
        for source in sorted(catalog, key=len, reverse=True):
            if source and source in output:
                output = output.replace(source, catalog[source])
        return output

    def missing_keys(self, language: str) -> set[str]:
        if language in {"zh", "en"}:
            return set()
        catalog = self.catalogs.get(language, {})
        return {
            source
            for source, english in self.catalogs["en"].items()
            if catalog.get(source, english) == english
        }
