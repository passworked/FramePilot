from __future__ import annotations

import json
from pathlib import Path
import re
import string
import unittest

from framepilot_i18n import LANGUAGE_OPTIONS, LocalizedMessage, Localizer


ROOT = Path(__file__).resolve().parent


def placeholders(text: str) -> set[str]:
    return {
        name
        for _literal, name, _format_spec, _conversion in string.Formatter().parse(text)
        if name is not None
    }


class LocalizationCatalogTests(unittest.TestCase):
    def test_all_locale_catalogs_cover_the_source_catalog(self) -> None:
        expected = set(
            json.loads(
                (ROOT / "locales" / "en.json").read_text(encoding="utf-8")
            )
        )
        for language, _label in LANGUAGE_OPTIONS:
            with self.subTest(language=language):
                data = json.loads(
                    (ROOT / "locales" / f"{language}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(set(data), expected)

    def test_catalog_keys_are_stable_semantic_identifiers(self) -> None:
        key_pattern = re.compile(
            r"^[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$"
        )
        for language, _label in LANGUAGE_OPTIONS:
            data = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(
                    encoding="utf-8"
                )
            )
            for key in data:
                with self.subTest(language=language, key=key):
                    self.assertRegex(key, key_pattern)
                    self.assertIsNone(re.search(r"[\u3400-\u9fff]", key))

    def test_all_translations_preserve_format_placeholders(self) -> None:
        english = json.loads(
            (ROOT / "locales" / "en.json").read_text(encoding="utf-8")
        )
        for language, _label in LANGUAGE_OPTIONS:
            data = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )
            for key, translated in data.items():
                with self.subTest(language=language, key=key):
                    self.assertEqual(
                        placeholders(english[key]),
                        placeholders(translated),
                    )

    def test_locales_do_not_leave_full_english_sentences_as_fallbacks(self) -> None:
        english = json.loads(
            (ROOT / "locales" / "en.json").read_text(encoding="utf-8")
        )
        for language, _label in LANGUAGE_OPTIONS:
            if language in {"zh", "en"}:
                continue
            data = json.loads(
                (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )
            for key, english_text in english.items():
                if len(english_text.split()) < 3:
                    continue
                with self.subTest(language=language, key=key):
                    self.assertNotEqual(data[key], english_text)

    def test_localizer_switches_language_and_formats_values(self) -> None:
        localizer = Localizer(ROOT / "locales")

        self.assertEqual(localizer.translate("ui.clear", "en"), "Clear")
        self.assertNotEqual(localizer.translate("清空", "ja"), "清空")
        self.assertIn(
            "7",
            localizer.format(
                "format.retry_in_minutes_min",
                "de",
                minutes=7,
            ),
        )

    def test_unknown_text_falls_back_without_crashing(self) -> None:
        localizer = Localizer(ROOT / "locales")

        self.assertEqual(localizer.translate("unregistered", "fr"), "unregistered")

    def test_target_frame_rate_change_event_has_no_chinese_residue(self) -> None:
        localizer = Localizer(ROOT / "locales")
        message = LocalizedMessage(
            "event.target_fps_lowered",
            {"old_fps": 72.0, "new_fps": 36.0},
        )

        self.assertEqual(
            localizer.localize_message(message, "en"),
            "Target frame rate lowered 72 → 36 FPS; "
            "waiting for the actual frame cadence to stabilize before "
            "predicting a resolution increase",
        )
        for language, _label in LANGUAGE_OPTIONS:
            if language == "zh":
                continue
            with self.subTest(language=language):
                translated = localizer.localize_message(message, language)
                self.assertNotIn("目标帧率降低", translated)
                self.assertNotIn("等待实际节拍稳定后预测升档", translated)
                self.assertNotIn("；", translated)
                if language != "ja":
                    self.assertIsNone(re.search(r"[\u3400-\u9fff]", translated))


if __name__ == "__main__":
    unittest.main()
