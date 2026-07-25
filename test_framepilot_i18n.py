from __future__ import annotations

import json
from pathlib import Path
import string
import unittest

from framepilot_i18n import LANGUAGE_OPTIONS, Localizer
from steamvr_adaptive_gui import ZH_EN


ROOT = Path(__file__).resolve().parent


def placeholders(text: str) -> set[str]:
    return {
        name
        for _literal, name, _format_spec, _conversion in string.Formatter().parse(text)
        if name is not None
    }


class LocalizationCatalogTests(unittest.TestCase):
    def test_all_locale_catalogs_cover_the_source_catalog(self) -> None:
        expected = set(ZH_EN)
        for language, _label in LANGUAGE_OPTIONS:
            if language in {"zh", "en"}:
                continue
            with self.subTest(language=language):
                data = json.loads(
                    (ROOT / "locales" / f"{language}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(set(data), expected)

    def test_all_translations_preserve_format_placeholders(self) -> None:
        for language, _label in LANGUAGE_OPTIONS:
            if language in {"zh", "en"}:
                continue
            data = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )
            for source, translated in data.items():
                with self.subTest(language=language, source=source):
                    self.assertEqual(placeholders(source), placeholders(translated))

    def test_locales_do_not_leave_full_english_sentences_as_fallbacks(self) -> None:
        for language, _label in LANGUAGE_OPTIONS:
            if language in {"zh", "en"}:
                continue
            data = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )
            for source, english in ZH_EN.items():
                if len(english.split()) < 3:
                    continue
                with self.subTest(language=language, source=source):
                    self.assertNotEqual(data[source], english)

    def test_localizer_switches_language_and_formats_values(self) -> None:
        localizer = Localizer(ZH_EN, ROOT / "locales")

        self.assertEqual(localizer.translate("清空", "en"), "Clear")
        self.assertNotEqual(localizer.translate("清空", "ja"), "清空")
        self.assertIn(
            "7",
            localizer.format("{minutes} 分钟后重试", "de", minutes=7),
        )

    def test_unknown_text_falls_back_without_crashing(self) -> None:
        localizer = Localizer(ZH_EN, ROOT / "locales")

        self.assertEqual(localizer.translate("unregistered", "fr"), "unregistered")


if __name__ == "__main__":
    unittest.main()
