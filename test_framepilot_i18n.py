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

    def test_coalesced_downshift_reason_is_localized(self) -> None:
        localizer = Localizer(ROOT / "locales")
        message = (
            "GPU 帧时间或重投影超过安全阈值; "
            "合并降档 10% 以减少 SteamVR 重建次数"
        )

        self.assertEqual(
            localizer.localize_message(message, "en"),
            "GPU frame time or reprojection exceeded the safety threshold; "
            "Coalesced downshift 10% to reduce SteamVR rebuilds",
        )
        for language, _label in LANGUAGE_OPTIONS:
            if language == "zh":
                continue
            with self.subTest(language=language):
                translated = localizer.localize_message(message, language)
                self.assertNotIn("GPU 帧时间或重投影超过安全阈值", translated)
                self.assertNotIn("合并降档", translated)
                self.assertNotIn("以减少 SteamVR 重建次数", translated)

    def test_dashboard_events_are_semantic_and_fully_localized(self) -> None:
        localizer = Localizer(ROOT / "locales")
        keys = (
            "event.dashboard_visible",
            "event.dashboard_status_unavailable",
            "event.dashboard_recovering",
            "event.dashboard_recovered",
        )
        simplified_only = re.compile(
            r"[个为与后发帧还载务录处认验证议档过户设备请仅时开启关闭"
            r"选择从对应统计窗稳复显隐总条达则进续传场优级线阶写读锁"
            r"删动项边压异码层]"
        )
        for language, _label in LANGUAGE_OPTIONS:
            if language == "zh":
                continue
            for key in keys:
                with self.subTest(language=language, key=key):
                    translated = localizer.localize_message(
                        LocalizedMessage(key),
                        language,
                    )
                    self.assertNotEqual(
                        translated,
                        localizer.translate(
                            "event.runtime_message_translation_incomplete",
                            language,
                        ),
                    )
                    if language == "ja":
                        self.assertIsNone(simplified_only.search(translated))
                    else:
                        self.assertIsNone(
                            re.search(r"[\u3400-\u9fff]", translated)
                        )

    def test_legacy_runtime_messages_never_leak_chinese_fragments(self) -> None:
        localizer = Localizer(ROOT / "locales")
        legacy = (
            "Dashboard 切换后的统计窗口与帧节拍已稳定；恢复自适应分辨率",
            "A/B 测试已取消: 场景应用发生变化",
            "VR 参数叠加层异常退出，代码 3",
        )
        for language, _label in LANGUAGE_OPTIONS:
            if language == "zh":
                continue
            for message in legacy:
                with self.subTest(language=language, message=message):
                    translated = localizer.localize_message(message, language)
                    if language == "ja":
                        self.assertIsNone(
                            re.search(
                                r"[个为与后发帧还载务录处认验证议档过户设备请仅时"
                                r"开启关闭选择从对应统计窗稳复显隐总条达则进续传场"
                                r"优级线阶写读锁删动项边压异码层]",
                                translated,
                            )
                        )
                    else:
                        self.assertIsNone(
                            re.search(r"[\u3400-\u9fff]", translated)
                        )

    def test_japanese_upload_summary_uses_natural_record_counters(self) -> None:
        localizer = Localizer(ROOT / "locales")
        translated = localizer.format(
            "format.upload_complete_accepted_accepted_duplicates_duplicate_record_s_9f34a9df",
            "ja",
            accepted=16,
            duplicates=0,
            batches=1,
        )
        self.assertEqual(
            translated,
            "アップロード完了：16 件を受信し、0 件は重複していました"
            "（全 1 バッチ）。",
        )
        self.assertNotIn("個のアイテム", translated)


if __name__ == "__main__":
    unittest.main()
