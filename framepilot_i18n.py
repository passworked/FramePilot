from __future__ import annotations

import json
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class LocalizedMessage:
    key: str
    values: dict[str, object] = field(default_factory=dict)


def resource_path(*parts: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parent
    return root.joinpath(*parts)


class Localizer:
    """Resolve semantic catalog keys, with value lookup for legacy UI call sites."""

    def __init__(
        self,
        locale_dir: Path | None = None,
    ) -> None:
        directory = locale_dir or resource_path("locales")
        self._raw_catalogs: dict[str, dict[str, str]] = {}
        for language, _label in LANGUAGE_OPTIONS:
            path = directory / f"{language}.json"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                raw = {}
            self._raw_catalogs[language] = (
                {
                    str(key): str(value)
                    for key, value in raw.items()
                    if isinstance(value, str) and value.strip()
                }
                if isinstance(raw, dict)
                else {}
            )
        english = self._raw_catalogs.get("en", {})
        chinese = self._raw_catalogs.get("zh", {})
        self.keys = frozenset(english) | frozenset(chinese)
        self.catalogs: dict[str, dict[str, str]] = {}
        for language, _label in LANGUAGE_OPTIONS:
            raw_catalog = self._raw_catalogs.get(language, {})
            self.catalogs[language] = {
                key: raw_catalog.get(
                    key,
                    english.get(key, chinese.get(key, key)),
                )
                for key in self.keys
            }
        self._key_by_translation: dict[str, str] = {}
        for catalog in self.catalogs.values():
            for key, translated in catalog.items():
                self._key_by_translation.setdefault(translated, key)

    def translate(self, text: str, language: str) -> str:
        key = text if text in self.keys else self._key_by_translation.get(text)
        if key is None:
            return text
        catalog = self.catalogs.get(language, self.catalogs.get("en", {}))
        return catalog.get(key, text)

    def format(self, text: str, language: str, **values: object) -> str:
        return self.translate(text, language).format(**values)

    def localize_message(
        self,
        message: str | LocalizedMessage,
        language: str,
    ) -> str:
        if isinstance(message, LocalizedMessage):
            return self.format(message.key, language, **message.values)
        exact_key = (
            message
            if message in self.keys
            else self._key_by_translation.get(message)
        )
        if exact_key is not None:
            return self.translate(exact_key, language)
        if language == "zh":
            return message
        catalog = self.catalogs.get(language, self.catalogs["en"])
        output = message
        chinese = self.catalogs.get("zh", {})
        sources = sorted(chinese.items(), key=lambda item: len(item[1]), reverse=True)
        for key, source in sources:
            if source and source in output:
                output = output.replace(source, catalog.get(key, source))
        return output

    def missing_keys(self, language: str) -> set[str]:
        return set(self.keys) - set(self._raw_catalogs.get(language, {}))
