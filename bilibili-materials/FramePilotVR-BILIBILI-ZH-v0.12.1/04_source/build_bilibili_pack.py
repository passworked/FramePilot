from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[3]
PACK = Path(__file__).resolve().parents[1]
IMAGES = PACK / "01_images"
VIDEO = PACK / "02_video"
COPY = PACK / "03_copy"
SOURCE = PACK / "04_source"
CAPTURES = SOURCE / "captures"
SLIDES = SOURCE / "video_slides"

FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")

INK = "#F4F7FB"
MUTED = "#AFC0D5"
CYAN = "#27D4FF"
MINT = "#5FF2CB"
VIOLET = "#A486FF"
ORANGE = "#FFB25F"
RED = "#FF7C91"
PANEL = "#0C131E"
BORDER = "#29445C"
NAVY = "#050911"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size=size)


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def fit_cover(image: Image.Image, size: tuple[int, int], anchor: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height)
    resized = image.resize(
        (round(image.width * ratio), round(image.height * ratio)),
        Image.Resampling.LANCZOS,
    )
    left = round(max(0, resized.width - target_w) * anchor[0])
    top = round(max(0, resized.height - target_h) * anchor[1])
    return resized.crop((left, top, left + target_w, top + target_h))


def fit_contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    ratio = min(size[0] / image.width, size[1] / image.height)
    return image.resize(
        (round(image.width * ratio), round(image.height * ratio)),
        Image.Resampling.LANCZOS,
    )


def rounded_paste(
    canvas: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int = 26,
    border: str = BORDER,
    shadow: int = 22,
) -> None:
    x1, y1, x2, y2 = box
    size = (x2 - x1, y2 - y1)
    fitted = fit_cover(source.convert("RGB"), size)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    if shadow:
        shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow_layer).rounded_rectangle(
            (x1, y1 + 10, x2, y2 + 10),
            radius=radius,
            fill=(0, 0, 0, 190),
        )
        canvas.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(shadow)))
    canvas.paste(fitted, (x1, y1), mask)
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=radius, outline=border, width=2)


def pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    size: int = 24,
    color: str = MINT,
    fill: str = "#0C2B23",
    outline: str = "#2B7B66",
) -> tuple[int, int, int, int]:
    f = font(size, bold=True)
    bbox = draw.textbbox((0, 0), text, font=f)
    width = bbox[2] - bbox[0] + 38
    height = bbox[3] - bbox[1] + 22
    x, y = xy
    rect = (x, y, x + width, y + height)
    draw.rounded_rectangle(rect, radius=height // 2, fill=fill, outline=outline, width=2)
    draw.text((x + 19, y + 11 - bbox[1]), text, font=f, fill=color)
    return rect


def brand(canvas: Image.Image, draw: ImageDraw.ImageDraw, icon: Image.Image, x: int = 72, y: int = 56) -> None:
    mark = fit_contain(icon.convert("RGBA"), (64, 64))
    canvas.alpha_composite(mark, (x, y))
    draw.text((x + 84, y + 2), "FramePilot VR", font=font(34, bold=True), fill=INK)
    draw.text((x + 85, y + 44), "STEAMVR DYNAMIC RESOLUTION", font=font(14, bold=True), fill=CYAN)


class _PreviewWorker:
    def submit_config(self, *_args) -> None:
        pass

    def submit_overlay_settings(self, *_args) -> None:
        pass

    def submit_collection_enabled(self, *_args) -> None:
        pass


def render_chinese_app(path: Path) -> Image.Image:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    os.environ["FRAMEPILOT_SUPPRESS_STEAMVR_AUTOSTART"] = "1"
    sys.path.insert(0, str(ROOT))

    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    import steamvr_adaptive_gui as gui

    with tempfile.TemporaryDirectory(prefix="framepilot-bilibili-") as settings_dir:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, settings_dir)
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setApplicationName("FramePilot VR")
        app.setFont(QFont("Microsoft YaHei UI", 10))
        app.setStyleSheet(gui.STYLE)

        class PreviewWindow(gui.MainWindow):
            def _start_worker(self) -> None:
                self.worker = _PreviewWorker()

            def _setup_tray(self) -> None:
                return

            def _sync_overlay_process(self) -> None:
                return

            def _send_overlay_settings(self) -> None:
                return

            def maybe_show_onboarding(self) -> None:
                return

            def closeEvent(self, event) -> None:  # noqa: N802
                event.accept()

        window = PreviewWindow(language_override="zh")
        window.resize(1240, 840)
        window.mode_combo.blockSignals(True)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("monitor"))
        window.mode_combo.blockSignals(False)
        window.arm_check.blockSignals(True)
        window.arm_check.setChecked(False)
        window.arm_check.blockSignals(False)
        window.steamvr_autostart_check.blockSignals(True)
        window.steamvr_autostart_check.setChecked(False)
        window.steamvr_autostart_check.blockSignals(False)
        window._update_write_status_banner()
        window.update_connection(True, "SteamVR 已连接")
        window.update_snapshot(
            {
                "gpu_p95_ms": 11.90,
                "cpu_p95_ms": 5.80,
                "budget_ms": 13.89,
                "refresh_hz": 72.0,
                "target_fps": 72.0,
                "target_divisor": 1,
                "resolution_scale": 118,
                "system_gpu_pct": 84.0,
                "system_cpu_pct": 28.0,
                "proposed_scale": 116,
                "app_key": "VRChat · Starlit Slumber",
                "render_width": 2000,
                "render_height": 1800,
                "write_count": 0,
                "reason": "GPU 帧时间接近预算，保持当前分辨率",
                "write_applied": False,
            }
        )
        window.append_event("success", "已连接 SteamVR，当前保持只读监控")
        window.append_event("info", "检测到 72 Hz 头显，目标帧预算 13.89 ms")
        window.show()
        for _ in range(12):
            app.processEvents()
        pixmap = window.grab()
        pixmap.save(str(path))
        window.hide()
        app.processEvents()
    return Image.open(path).convert("RGB")


def render_chinese_osd(path: Path) -> Image.Image:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtGui import QGuiApplication
    from steamvr_overlay import DEFAULT_FIELDS, render_osd

    qt_app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    sample = {
        "gpu_p95_ms": 11.90,
        "cpu_p95_ms": 5.80,
        "budget_ms": 13.89,
        "refresh_hz": 72.0,
        "application_fps": 72.0,
        "target_fps": 72.0,
        "frame_interval_p95_ms": 13.20,
        "system_gpu_pct": 84.0,
        "system_cpu_pct": 28.0,
        "render_width": 2000,
        "render_height": 1800,
        "resolution_scale": 118,
        "proposed_scale": 116,
        "decision": "hold",
        "reprojection_pct": 0.0,
        "vrc_world_short": "Starlit Slumber",
        "vrc_population": 12,
        "vrc_context_ready": True,
        "vrc_profile_safe_scale": 116,
    }
    rendered = render_osd(sample, list(DEFAULT_FIELDS), language="zh")
    rendered.save(str(path))
    qt_app.processEvents()
    return Image.open(path).convert("RGB")


def new_canvas(background: Image.Image, size: tuple[int, int] = (1920, 1080), veil: int = 80) -> Image.Image:
    canvas = fit_cover(background.convert("RGB"), size, (0.5, 0.48)).convert("RGBA")
    canvas.alpha_composite(Image.new("RGBA", size, (1, 5, 13, veil)))
    return canvas


def footer(draw: ImageDraw.ImageDraw, text: str = "Windows · SteamVR · 简体中文 · v0.12.1") -> None:
    draw.line((72, 1010, 1848, 1010), fill="#274057", width=2)
    draw.text((72, 1029), text, font=font(18), fill=MUTED)


def save_cover(
    background: Image.Image,
    app_capture: Image.Image,
    icon: Image.Image,
    size: tuple[int, int],
    filename: str,
) -> Path:
    width, height = size
    canvas = new_canvas(background, size, veil=92)
    draw = ImageDraw.Draw(canvas)
    scale = width / 1920

    def s(value: int) -> int:
        return round(value * scale)

    mark = fit_contain(icon.convert("RGBA"), (s(70), s(70)))
    canvas.alpha_composite(mark, (s(72), s(58)))
    draw.text((s(160), s(63)), "FramePilot VR", font=font(s(38), bold=True), fill=INK)
    pill(draw, (width - s(330), s(62)), "30 秒看懂", size=s(23))

    if width / height > 1.5:
        draw.text((s(76), s(238)), "VR 掉帧？", font=font(s(92), bold=True), fill=INK)
        draw.text((s(76), s(356)), "让分辨率自己调", font=font(s(72), bold=True), fill=MINT)
        draw.text(
            (s(81), s(468)),
            "看懂 GPU 帧预算，再决定画质该升还是该降",
            font=font(s(27)),
            fill="#D8E3EF",
        )
        rounded_paste(canvas, app_capture, (s(900), s(220), s(1848), s(867)), radius=s(28))
        pill(draw, (s(82), s(585)), "默认只读", size=s(22))
        pill(
            draw,
            (s(82), s(657)),
            "授权才写入",
            size=s(22),
            color=CYAN,
            fill="#101F32",
            outline="#356895",
        )
        draw.text((s(82), height - s(92)), "STEAMVR 动态分辨率控制器", font=font(s(23), bold=True), fill=MUTED)
    else:
        draw.text((s(76), s(206)), "VR 掉帧？", font=font(s(80), bold=True), fill=INK)
        draw.text((s(76), s(310)), "让分辨率自己调", font=font(s(63), bold=True), fill=MINT)
        draw.text(
            (s(81), s(410)),
            "看懂帧预算，再决定画质升降",
            font=font(s(25)),
            fill="#D8E3EF",
        )
        rounded_paste(canvas, app_capture, (s(75), s(525), width - s(75), height - s(120)), radius=s(26))
        pill(draw, (s(82), height - s(165)), "默认只读 · 授权才写入", size=s(20))

    out = IMAGES / filename
    canvas.convert("RGB").save(out, quality=95, optimize=True)
    canvas.convert("RGB").save(out.with_suffix(".jpg"), quality=92, optimize=True, progressive=True)
    return out


def scene_base(background: Image.Image, icon: Image.Image, badge: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    canvas = new_canvas(background)
    draw = ImageDraw.Draw(canvas)
    brand(canvas, draw, icon)
    pill(draw, (1585, 63), badge, size=21)
    return canvas, draw


def save_scene_1(background: Image.Image, icon: Image.Image) -> Path:
    canvas, draw = scene_base(background, icon, "30 秒看懂")
    draw.text((78, 238), "VR 掉帧？", font=font(108, bold=True), fill=INK)
    draw.text((78, 375), "先别盲调画质", font=font(78, bold=True), fill=MINT)
    draw.text((84, 505), "你真正需要看的，是每一帧的时间预算。", font=font(31), fill="#D8E3EF")
    for x, value, label, color in (
        (84, "11.90 ms", "GPU P95", CYAN),
        (380, "13.89 ms", "帧预算", MINT),
        (676, "72 FPS", "目标帧率", VIOLET),
    ):
        draw.rounded_rectangle((x, 610, x + 255, 740), radius=22, fill="#09131FCC", outline="#2A435A", width=2)
        draw.text((x + 22, 635), value, font=font(32, bold=True), fill=color)
        draw.text((x + 22, 690), label, font=font(19), fill=MUTED)
    draw.text((84, 835), "FramePilot VR", font=font(44, bold=True), fill=INK)
    draw.text((84, 895), "SteamVR 动态分辨率控制器", font=font(26), fill=MUTED)
    footer(draw)
    out = SLIDES / "01_hook.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_scene_2(background: Image.Image, icon: Image.Image, osd: Image.Image) -> Path:
    canvas, draw = scene_base(background, icon, "VR 内 OSD")
    draw.text((78, 225), "先看清瓶颈", font=font(78, bold=True), fill=CYAN)
    draw.text((82, 338), "GPU、CPU、帧预算、重投影", font=font(34), fill=INK)
    draw.text((82, 398), "不用摘下头显，也能看见当前负载。", font=font(28), fill=MUTED)
    card = Image.new("RGBA", (850, 550), (2, 5, 9, 230))
    osd_fit = fit_contain(osd, (790, 500))
    card.alpha_composite(osd_fit.convert("RGBA"), ((850 - osd_fit.width) // 2, 28))
    rounded_paste(canvas, card, (980, 235, 1830, 785), radius=28)
    for y, label, color in (
        (545, "GPU P95：判断是否超出帧预算", CYAN),
        (630, "CPU P95：识别 CPU 瓶颈", VIOLET),
        (715, "分辨率：显示当前值与建议值", MINT),
    ):
        draw.ellipse((88, y + 6, 108, y + 26), fill=color)
        draw.text((130, y), label, font=font(25), fill="#DDE7F1")
    footer(draw)
    out = SLIDES / "02_osd.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_scene_3(background: Image.Image, icon: Image.Image, app_capture: Image.Image) -> Path:
    canvas, draw = scene_base(background, icon, "自动分辨率")
    draw.text((78, 210), "重场景，自动降", font=font(70, bold=True), fill=ORANGE)
    draw.text((78, 307), "有余量，谨慎升", font=font(70, bold=True), fill=MINT)
    draw.text((83, 420), "按帧预算逐步调整 SteamVR resolutionScale", font=font(29), fill="#D8E3EF")
    rounded_paste(canvas, app_capture, (830, 205, 1838, 865), radius=28)
    draw.rounded_rectangle((82, 544, 720, 738), radius=25, fill="#09131FDD", outline="#2A435A", width=2)
    draw.text((116, 579), "高负载", font=font(24, bold=True), fill=ORANGE)
    draw.text((290, 575), "→", font=font(36, bold=True), fill=MUTED)
    draw.text((370, 579), "分辨率下调", font=font(24, bold=True), fill=INK)
    draw.line((118, 647, 650, 647), fill="#354B60", width=4)
    draw.line((118, 647, 430, 647), fill=ORANGE, width=5)
    draw.polygon(((424, 635), (450, 647), (424, 659)), fill=ORANGE)
    draw.text((116, 681), "稳定窗口 + 冷却时间，减少来回抖动", font=font(20), fill=MUTED)
    footer(draw)
    out = SLIDES / "03_auto.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_scene_4(background: Image.Image, icon: Image.Image, app_capture: Image.Image) -> Path:
    canvas, draw = scene_base(background, icon, "安全优先")
    draw.text((78, 218), "默认只读", font=font(78, bold=True), fill=MINT)
    draw.text((78, 330), "授权后才写入", font=font(70, bold=True), fill=INK)
    draw.text((83, 442), "先观察，再单步验证；退出时恢复启动值。", font=font(29), fill="#D8E3EF")
    rounded_paste(canvas, app_capture, (850, 208, 1838, 858), radius=28)
    points = [
        ("01", "默认不修改 SteamVR", CYAN),
        ("02", "写入权限需要手动开启", MINT),
        ("03", "CPU 受限时不盲目降画质", VIOLET),
    ]
    for index, (number, label, color) in enumerate(points):
        y = 555 + index * 92
        draw.rounded_rectangle((84, y, 145, y + 61), radius=18, fill="#0A1826", outline=color, width=2)
        draw.text((99, y + 13), number, font=font(22, bold=True), fill=color)
        draw.text((175, y + 12), label, font=font(25), fill=INK)
    footer(draw)
    out = SLIDES / "04_safety.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_scene_5(background: Image.Image, icon: Image.Image) -> Path:
    canvas, draw = scene_base(background, icon, "P95 帧时间")
    draw.text((78, 208), "不只看平均值", font=font(78, bold=True), fill=VIOLET)
    draw.text((82, 321), "用 P95 看见真实卡顿", font=font(48, bold=True), fill=INK)
    draw.text((84, 400), "再用稳定窗口和冷却时间，避免分辨率频繁抖动。", font=font(28), fill=MUTED)
    chart = (85, 535, 1835, 855)
    draw.rounded_rectangle(chart, radius=28, fill="#07111CDD", outline="#2A435A", width=2)
    for i in range(5):
        y = 580 + i * 55
        draw.line((130, y, 1785, y), fill="#203449", width=2)
    points: list[tuple[int, int]] = []
    for i in range(75):
        x = 135 + i * 22
        base = 710 + math.sin(i * 0.33) * 30 + math.sin(i * 0.11) * 18
        spike = -125 if i in {17, 36, 52} else -70 if i in {18, 53} else 0
        points.append((x, round(base + spike)))
    draw.line(points, fill=CYAN, width=5, joint="curve")
    draw.line((130, 640, 1785, 640), fill=MINT, width=4)
    draw.text((1420, 592), "帧预算 13.89 ms", font=font(20, bold=True), fill=MINT)
    draw.text((130, 785), "GPU P95", font=font(22, bold=True), fill=CYAN)
    draw.text((295, 785), "捕捉偶发尖峰", font=font(21), fill=MUTED)
    footer(draw)
    out = SLIDES / "05_p95.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_scene_6(background: Image.Image, icon: Image.Image, app_capture: Image.Image) -> Path:
    canvas = new_canvas(background, veil=105)
    draw = ImageDraw.Draw(canvas)
    mark = fit_contain(icon.convert("RGBA"), (116, 116))
    canvas.alpha_composite(mark, (86, 174))
    draw.text((236, 176), "FramePilot VR", font=font(76, bold=True), fill=INK)
    draw.text((241, 276), "让每一帧，更有把握", font=font(49, bold=True), fill=MINT)
    draw.text((241, 360), "Windows · SteamVR · 简体中文", font=font(27), fill="#D8E3EF")
    rounded_paste(canvas, app_capture, (1010, 142, 1840, 790), radius=30)
    pill(draw, (238, 500), "完整使用说明见简介", size=28)
    draw.text((241, 594), "先只读观察，再决定是否开启自动调节。", font=font(27), fill=MUTED)
    draw.rounded_rectangle((238, 698, 842, 794), radius=24, fill="#081521DD", outline="#2D5069", width=2)
    draw.text((276, 726), "OSD  ·  AUTO RESOLUTION  ·  P95", font=font(22, bold=True), fill=CYAN)
    footer(draw)
    out = SLIDES / "06_outro.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def make_audio(path: Path, duration: float = 30.0, sample_rate: int = 48_000) -> None:
    total = round(duration * sample_rate)
    chords = [
        (55.00, 82.41, 110.00),
        (49.00, 73.42, 98.00),
        (65.41, 98.00, 130.81),
        (43.65, 65.41, 87.31),
        (55.00, 82.41, 123.47),
        (55.00, 82.41, 110.00),
    ]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        chunk = bytearray()
        for index in range(total):
            t = index / sample_rate
            section = min(5, int(t // 5))
            local = t - section * 5
            chord = chords[section]
            pad_env = min(1.0, local / 0.8, (5.0 - local) / 0.65)
            pad = sum(math.sin(2 * math.pi * frequency * t + section * 0.23) for frequency in chord) / 3
            shimmer = math.sin(2 * math.pi * chord[1] * 4 * t) * 0.08
            beat_phase = t % 1.25
            kick = math.sin(2 * math.pi * (54 - 24 * min(beat_phase / 0.22, 1)) * beat_phase)
            kick *= math.exp(-beat_phase * 17)
            transition_phase = local
            hit = math.sin(2 * math.pi * 180 * transition_phase) * math.exp(-transition_phase * 12)
            value = (0.17 * pad * pad_env) + (0.035 * shimmer * pad_env) + (0.14 * kick) + (0.07 * hit)
            master = min(1.0, t / 0.8, (duration - t) / 1.0)
            sample = max(-1.0, min(1.0, value * master))
            left = round(sample * 32767)
            right = round(sample * 0.94 * 32767)
            chunk.extend(struct.pack("<hh", left, right))
            if len(chunk) >= 65_536:
                wav.writeframes(chunk)
                chunk.clear()
        if chunk:
            wav.writeframes(chunk)


def build_video(slides: list[Path], audio: Path, output: Path) -> None:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("Run this script with the project .venv; imageio-ffmpeg is required.") from exc
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
    for slide in slides:
        command.extend(["-loop", "1", "-framerate", "30", "-t", "5", "-i", str(slide)])
    command.extend(["-i", str(audio)])
    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(slides)):
        label = f"v{index}"
        pan = "iw/2-(iw/zoom/2)" if index % 2 == 0 else "iw/2-(iw/zoom/2)+8*sin(on/45)"
        filters.append(
            f"[{index}:v]scale=2048:1152,"
            f"zoompan=z='min(zoom+0.00012,1.035)':x='{pan}':"
            f"y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,"
            "fade=t=in:st=0:d=0.30,fade=t=out:st=4.68:d=0.32,"
            f"format=yuv420p,setsar=1[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={len(slides)}:v=1:a=0[outv]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            f"{len(slides)}:a",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-t",
            "30",
            str(output),
        ]
    )
    subprocess.run(command, check=True)


def make_preview(slides: list[Path], covers: list[Path], output: Path) -> None:
    thumbs = [Image.open(path).convert("RGB") for path in covers + slides]
    canvas = Image.new("RGB", (1500, 1000), NAVY)
    draw = ImageDraw.Draw(canvas)
    draw.text((40, 24), "FramePilot VR · B站中文素材预览", font=font(38, bold=True), fill=INK)
    for index, image in enumerate(thumbs):
        x = 40 + (index % 3) * 480
        y = 92 + (index // 3) * 285
        thumb = fit_cover(image, (450, 250))
        canvas.paste(thumb, (x, y))
        draw.rounded_rectangle((x + 12, y + 12, x + 60, y + 52), radius=14, fill="#07111CDD")
        draw.text((x + 27, y + 15), str(index + 1), font=font(18, bold=True), fill=MINT)
    canvas.save(output, quality=92)


def write_manifest() -> None:
    manifest = PACK / "MANIFEST_SHA256.txt"
    lines = []
    for path in sorted(PACK.rglob("*")):
        if not path.is_file() or path == manifest:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(PACK).as_posix()}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    for directory in (IMAGES, VIDEO, COPY, CAPTURES, SLIDES):
        directory.mkdir(parents=True, exist_ok=True)

    background_path = CAPTURES / "imagegen-bilibili-background.png"
    if not background_path.exists():
        raise FileNotFoundError(
            f"Missing ImageGen background: {background_path}. Copy the generated background there first."
        )

    background = Image.open(background_path).convert("RGB")
    icon = Image.open(ROOT / "assets" / "framepilot-vr-icon.png").convert("RGBA")
    app_capture = render_chinese_app(CAPTURES / "app-zh-actual.png")
    osd = render_chinese_osd(CAPTURES / "osd-zh-actual.png")

    covers = [
        save_cover(background, app_capture, icon, (1920, 1080), "FramePilotVR_Bilibili_Cover_16x9.png"),
        save_cover(background, app_capture, icon, (1600, 1200), "FramePilotVR_Bilibili_Cover_4x3.png"),
    ]
    slides = [
        save_scene_1(background, icon),
        save_scene_2(background, icon, osd),
        save_scene_3(background, icon, app_capture),
        save_scene_4(background, icon, app_capture),
        save_scene_5(background, icon),
        save_scene_6(background, icon, app_capture),
    ]
    audio = CAPTURES / "original-electronic-bed.wav"
    make_audio(audio)
    build_video(slides, audio, VIDEO / "FramePilotVR_Bilibili_ZH_30s_1080p.mp4")
    audio.unlink(missing_ok=True)
    make_preview(slides, covers, IMAGES / "preview_contact_sheet.jpg")
    write_manifest()
    print(f"Built {len(covers)} covers, {len(slides)} scenes, and a 30-second video in {PACK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
