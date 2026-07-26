from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[3]
PACK = Path(__file__).resolve().parents[1]
IMAGES = PACK / "01_images"
VIDEO = PACK / "02_video"
SOURCE = PACK / "04_source"
CAPTURES = SOURCE / "captures"
VIDEO_SLIDES = SOURCE / "video_slides"

FONT_REGULAR = Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf")
FONT_SERIF = Path(r"C:\Windows\Fonts\NotoSerifJP-VF.ttf")

INK = "#F4F7FB"
MUTED = "#9BB0C7"
PANEL = "#111925"
PANEL_2 = "#0C131E"
BORDER = "#26384C"
CYAN = "#27D4FF"
MINT = "#5FF2CB"
VIOLET = "#A486FF"
ORANGE = "#FFB25F"
NAVY = "#070B12"


def font(size: int, serif: bool = False, weight: int = 500) -> ImageFont.FreeTypeFont:
    path = FONT_SERIF if serif else FONT_REGULAR
    face = ImageFont.truetype(str(path), size=size)
    face.set_variation_by_axes([weight])
    return face


def gradient(size: tuple[int, int], left: str = "#060A11", right: str = "#10142A") -> Image.Image:
    width, height = size
    a = Image.new("RGB", size, left)
    b = Image.new("RGB", size, right)
    mask = Image.new("L", size)
    px = mask.load()
    for x in range(width):
        value = int(255 * (x / max(1, width - 1)) ** 1.1)
        for y in range(height):
            px[x, y] = value
    return Image.composite(b, a, mask)


def glow(canvas: Image.Image, xy: tuple[int, int], radius: int, color: str, alpha: int = 100) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x, y = xy
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*hex_rgb(color), alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    canvas.alpha_composite(layer)


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def fit_cover(image: Image.Image, size: tuple[int, int], anchor_y: float = 0.5) -> Image.Image:
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height)
    resized = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    max_top = max(0, resized.height - target_h)
    top = round(max_top * anchor_y)
    return resized.crop((left, top, left + target_w, top + target_h))


def fit_contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    ratio = min(target_w / image.width, target_h / image.height)
    return image.resize((round(image.width * ratio), round(image.height * ratio)), Image.Resampling.LANCZOS)


def rounded_paste(
    canvas: Image.Image,
    image: Image.Image,
    box: tuple[int, int, int, int],
    radius: int = 24,
    border: str | None = BORDER,
    border_width: int = 2,
    shadow: int = 22,
) -> None:
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    fitted = fit_cover(image.convert("RGB"), (width, height))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)

    if shadow:
        shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.rounded_rectangle(
            (x1, y1 + 8, x2, y2 + 8),
            radius=radius,
            fill=(0, 0, 0, 170),
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow))
        canvas.alpha_composite(shadow_layer)

    canvas.paste(fitted, (x1, y1), mask)
    if border:
        ImageDraw.Draw(canvas).rounded_rectangle(box, radius=radius, outline=border, width=border_width)


def pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill: str = "#10281F",
    outline: str = "#276F5D",
    color: str = MINT,
    size: int = 26,
    padding_x: int = 22,
    padding_y: int = 11,
) -> tuple[int, int, int, int]:
    f = font(size)
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=f)
    width = bbox[2] - bbox[0] + padding_x * 2
    height = bbox[3] - bbox[1] + padding_y * 2
    rect = (x, y, x + width, y + height)
    draw.rounded_rectangle(rect, radius=height // 2, fill=fill, outline=outline, width=2)
    draw.text((x + padding_x, y + padding_y - bbox[1]), text, font=f, fill=color)
    return rect


def brand(draw: ImageDraw.ImageDraw, icon: Image.Image, canvas: Image.Image, y: int = 54) -> None:
    icon_small = fit_contain(icon.convert("RGBA"), (62, 62))
    canvas.alpha_composite(icon_small, (62, y))
    draw.text((140, y + 4), "FramePilot VR", font=font(34), fill=INK)
    draw.text((140, y + 43), "SteamVR Dynamic Resolution Controller", font=font(15), fill=MUTED)


def feature_number(draw: ImageDraw.ImageDraw, number: str, label: str, x: int = 68, y: int = 153) -> None:
    draw.text((x, y), number, font=font(24), fill=MINT)
    draw.text((x + 62, y + 1), label, font=font(22), fill=MUTED)


def footer(draw: ImageDraw.ImageDraw, text: str = "Windows / SteamVR  ·  v0.11.0") -> None:
    draw.line((66, 1134, 1134, 1134), fill="#213247", width=2)
    draw.text((66, 1150), text, font=font(18), fill=MUTED)
    draw.text((1074, 1148), "FP", font=font(20), fill=MINT)


def new_square() -> Image.Image:
    canvas = gradient((1200, 1200)).convert("RGBA")
    glow(canvas, (1030, 90), 330, VIOLET, 70)
    glow(canvas, (70, 1080), 290, CYAN, 48)
    return canvas


def safe_app_capture(path: Path) -> Image.Image:
    image = Image.open(path).convert("RGB")
    safe_bottom = min(image.height, round(image.height * 0.77))
    return image.crop((0, 0, image.width, safe_bottom))


def render_osd_japanese(path: Path) -> Image.Image:
    sys.path.insert(0, str(ROOT))
    from PySide6.QtGui import QGuiApplication
    from steamvr_overlay import DEFAULT_FIELDS, render_osd

    qt_app = QGuiApplication.instance() or QGuiApplication([])
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
    rendered = render_osd(sample, list(DEFAULT_FIELDS), language="ja")
    rendered.save(str(path))
    qt_app.processEvents()
    return Image.open(path).convert("RGBA")


def save_square_cover(icon: Image.Image, app_ja: Image.Image) -> Path:
    canvas = new_square()
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    pill(draw, (930, 58), "無料配布", size=22)
    draw.text((66, 170), "VRのフレームを、", font=font(77), fill=INK)
    draw.text((66, 263), "もっと賢く。", font=font(92), fill=MINT)
    draw.text((70, 388), "負荷に合わせて画質を調整する、\nSteamVR向け動的解像度コントローラー。", font=font(31), fill="#D4E0ED", spacing=16)
    rounded_paste(canvas, app_ja, (65, 550, 1135, 1055), radius=28)
    pill(draw, (78, 1000), "日本語対応", size=20)
    pill(draw, (267, 1000), "OSD表示", size=20, fill="#111E33", outline="#2D5E91", color=CYAN)
    pill(draw, (438, 1000), "匿名テレメトリ", size=20, fill="#211A37", outline="#6550A7", color=VIOLET)
    footer(draw)
    out = IMAGES / "00_cover_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_square_osd(icon: Image.Image, osd: Image.Image, vr_bg: Image.Image) -> Path:
    canvas = fit_cover(vr_bg.convert("RGB"), (1200, 1200), 0.48).convert("RGBA")
    veil = Image.new("RGBA", canvas.size, (2, 7, 15, 138))
    canvas.alpha_composite(veil)
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    feature_number(draw, "01", "VR OSD")
    draw.text((66, 207), "VR内で、\n今の負荷が見える。", font=font(68), fill=INK, spacing=6)
    draw.text((70, 392), "FPS・GPU/CPU・解像度・再投影・\nVRChatコンテキストを、ヘッドセット内に表示。", font=font(27), fill="#D9E4F0", spacing=12)
    osd_card = Image.new("RGBA", (1040, 510), (3, 5, 8, 228))
    osd_fit = fit_contain(osd, (975, 440))
    osd_card.alpha_composite(osd_fit, ((1040 - osd_fit.width) // 2, 36))
    rounded_paste(canvas, osd_card, (80, 557, 1120, 1067), radius=30, border="#35516F")
    pill(draw, (82, 1030), "実際のOSDレンダリング", size=18, fill="#111E2B", outline="#315068", color=CYAN)
    footer(draw)
    out = IMAGES / "01_osd_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_square_auto(icon: Image.Image, app_ja: Image.Image) -> Path:
    canvas = new_square()
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    feature_number(draw, "02", "AUTO RESOLUTION")
    draw.text((66, 205), "負荷に合わせて、\n解像度を自動調整。", font=font(66), fill=INK, spacing=8)
    draw.text((70, 385), "フレーム予算を監視し、SteamVRの resolutionScale を段階的に調整。", font=font(25), fill="#D5E0ED")
    rounded_paste(canvas, app_ja, (66, 470, 1134, 1005), radius=28)
    pill(draw, (88, 960), "高負荷  →  解像度 ↓", size=21, fill="#2A1916", outline="#8A4E3F", color=ORANGE)
    pill(draw, (420, 960), "余裕  →  解像度 ↑", size=21, fill="#10281F", outline="#276F5D", color=MINT)
    draw.text((70, 1063), "※ 変更はユーザーが許可した実行中のみ。次回起動時は再ロックされます。", font=font(18), fill=MUTED)
    footer(draw)
    out = IMAGES / "02_auto_resolution_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_square_telemetry(icon: Image.Image, website: Image.Image) -> Path:
    canvas = new_square()
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    feature_number(draw, "03", "WORLD BENCH")
    draw.text((66, 205), "匿名データを、\nみんなの知見へ。", font=font(66), fill=INK, spacing=8)
    draw.text((70, 384), "実プレイの集計から、ワールドごとの負荷傾向を共有。", font=font(27), fill="#D5E0ED")
    rounded_paste(canvas, website, (66, 470, 1134, 1006), radius=28)
    pill(draw, (88, 960), "2,380 遥測記録", size=20, fill="#10281F", outline="#276F5D", color=MINT)
    pill(draw, (348, 960), "匿名集計", size=20, fill="#111E33", outline="#2D5E91", color=CYAN)
    draw.text((70, 1055), "プレイヤーID・インスタンスID・マシン名・ハードウェア指紋は送信しません。", font=font(18), fill=MUTED)
    footer(draw)
    out = IMAGES / "03_world_bench_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_square_fullframe(icon: Image.Image, app_ja: Image.Image) -> Path:
    canvas = new_square()
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    feature_number(draw, "04", "FRAME BUDGET")
    draw.text((66, 205), "目標は、いつでも\nフルフレーム。", font=font(70), fill=INK, spacing=5)
    draw.text((70, 394), "重い場面では画質を下げ、余裕が戻れば上げる。", font=font(28), fill="#D5E0ED")

    rounded_paste(canvas, app_ja, (66, 478, 1134, 933), radius=28)
    draw.rounded_rectangle((118, 965, 1082, 1060), radius=24, fill="#0E1723", outline="#2A4057", width=2)
    nodes = [
        (150, "負荷を計測", CYAN),
        (420, "予算と比較", VIOLET),
        (690, "解像度を調整", MINT),
        (965, "安定を待つ", ORANGE),
    ]
    for index, (x, text, color) in enumerate(nodes):
        draw.ellipse((x - 11, 997, x + 11, 1019), fill=color)
        bbox = draw.textbbox((0, 0), text, font=font(20))
        draw.text((x - (bbox[2] - bbox[0]) // 2, 1028), text, font=font(20), fill=INK)
        if index < len(nodes) - 1:
            draw.line((x + 20, 1008, nodes[index + 1][0] - 20, 1008), fill="#466078", width=4)
            draw.polygon(
                (
                    (nodes[index + 1][0] - 26, 1000),
                    (nodes[index + 1][0] - 14, 1008),
                    (nodes[index + 1][0] - 26, 1016),
                ),
                fill="#466078",
            )
    draw.text((70, 1082), "※ ゲーム・CPUボトルネック・回線等により、目標FPSを維持できない場合があります。", font=font(17), fill=MUTED)
    footer(draw)
    out = IMAGES / "04_full_frame_goal_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_square_languages(
    icon: Image.Image,
    app_ja: Image.Image,
    app_en: Image.Image,
    app_de: Image.Image,
) -> Path:
    canvas = new_square()
    draw = ImageDraw.Draw(canvas)
    brand(draw, icon, canvas)
    feature_number(draw, "05", "LANGUAGES")
    draw.text((66, 205), "7言語に対応。", font=font(76), fill=INK)
    draw.text((70, 308), "いつもの言葉で、すぐに使える。", font=font(30), fill="#D5E0ED")
    cards = [
        (app_ja, (66, 392, 764, 654), "日本語"),
        (app_en, (438, 610, 1134, 872), "English"),
        (app_de, (66, 827, 764, 1089), "Deutsch"),
    ]
    for image, box, label in cards:
        rounded_paste(canvas, image, box, radius=22, shadow=12)
        pill(draw, (box[0] + 18, box[1] + 17), label, size=18, fill="#07111CCC", outline="#33526E", color=INK)
    draw.text(
        (790, 380),
        "日本語\n英語\n中国語\n韓国語\nフランス語\nドイツ語\nスペイン語",
        font=font(24),
        fill=INK,
        spacing=4,
    )
    footer(draw, "7 languages  ·  Windows / SteamVR  ·  v0.11.0")
    out = IMAGES / "05_multilanguage_square_1200.png"
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_wide_scene(
    filename: str,
    icon: Image.Image,
    title: str,
    subtitle: str,
    image: Image.Image | None,
    *,
    accent: str = MINT,
    badge: str | None = None,
    background: Image.Image | None = None,
    note: str | None = None,
    image_box: tuple[int, int, int, int] = (875, 165, 1845, 900),
) -> Path:
    if background is None:
        canvas = gradient((1920, 1080)).convert("RGBA")
        glow(canvas, (1720, 120), 460, VIOLET, 62)
        glow(canvas, (80, 970), 380, CYAN, 44)
    else:
        canvas = fit_cover(background.convert("RGB"), (1920, 1080), 0.5).convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", canvas.size, (1, 5, 13, 130)))
    draw = ImageDraw.Draw(canvas)
    icon_small = fit_contain(icon.convert("RGBA"), (72, 72))
    canvas.alpha_composite(icon_small, (80, 72))
    draw.text((174, 80), "FramePilot VR", font=font(40), fill=INK)
    if badge:
        pill(draw, (1545, 76), badge, size=23)
    draw.text((80, 206), title, font=font(68), fill=accent, spacing=8)
    draw.text((84, 395), subtitle, font=font(29), fill="#D8E3EF", spacing=12)
    if image is not None:
        rounded_paste(canvas, image, image_box, radius=30, shadow=28)
    if note:
        draw.text((84, 943), note, font=font(18), fill=MUTED)
    draw.line((80, 1018, 1840, 1018), fill="#24384C", width=2)
    draw.text((80, 1034), "Windows / SteamVR  ·  v0.11.0", font=font(18), fill=MUTED)
    out = VIDEO_SLIDES / filename
    canvas.convert("RGB").save(out, quality=95)
    return out


def save_video_slides(
    icon: Image.Image,
    app_ja: Image.Image,
    website: Image.Image,
    osd: Image.Image,
    vr_bg: Image.Image,
    languages_square: Image.Image,
) -> list[Path]:
    slides = []
    intro = save_wide_scene(
        "01_intro.png",
        icon,
        "VRのフレームを、\nもっと賢く。",
        "SteamVR向け\n動的解像度コントローラー",
        app_ja,
        badge="無料配布",
        image_box=(820, 245, 1840, 819),
    )
    slides.append(intro)

    osd_composite = fit_cover(vr_bg.convert("RGB"), (970, 735), 0.5).convert("RGBA")
    osd_composite.alpha_composite(Image.new("RGBA", osd_composite.size, (0, 0, 0, 92)))
    osd_fit = fit_contain(osd, (890, 620))
    osd_composite.alpha_composite(osd_fit, ((970 - osd_fit.width) // 2, (735 - osd_fit.height) // 2))
    slides.append(
        save_wide_scene(
            "02_osd.png",
            icon,
            "VR内で、\n今の負荷が見える。",
            "FPS / GPU・CPU / 解像度 / 再投影\n必要な情報をヘッドセット内へ。",
            osd_composite,
            accent=CYAN,
            badge="OSD表示",
            background=vr_bg,
        )
    )
    slides.append(
        save_wide_scene(
            "03_auto.png",
            icon,
            "負荷に合わせて、\n解像度を自動調整。",
            "重い場面では下げ、\n余裕が戻れば慎重に上げる。",
            app_ja,
            accent=MINT,
            badge="AUTO RESOLUTION",
            image_box=(820, 245, 1840, 819),
        )
    )
    slides.append(
        save_wide_scene(
            "04_world_bench.png",
            icon,
            "匿名データを、\nみんなの知見へ。",
            "実プレイの集計から、\nワールドごとの負荷傾向を共有。",
            website,
            accent=VIOLET,
            badge="WORLD BENCH",
            note="プレイヤーID・インスタンスID・マシン名・ハードウェア指紋は送信しません。",
            image_box=(820, 245, 1840, 819),
        )
    )
    slides.append(
        save_wide_scene(
            "05_fullframe.png",
            icon,
            "目標は、いつでも\nフルフレーム。",
            "フレーム予算を監視して、\n画質と安定性のバランスを調整。",
            app_ja,
            accent=ORANGE,
            badge="FRAME BUDGET",
            note="※ ゲーム・CPUボトルネック・回線等により、目標FPSを維持できない場合があります。",
            image_box=(820, 245, 1840, 819),
        )
    )
    slides.append(
        save_wide_scene(
            "06_languages.png",
            icon,
            "7言語に対応。",
            "日本語 / 英語 / 中国語 / 韓国語\nフランス語 / ドイツ語 / スペイン語",
            languages_square,
            accent=CYAN,
            badge="MULTILINGUAL",
            image_box=(1100, 170, 1790, 860),
        )
    )
    slides.append(
        save_wide_scene(
            "07_outro.png",
            icon,
            "FramePilot VR",
            "SteamVR向け動的解像度コントローラー\nBOOTHにて無料配布",
            app_ja,
            accent=MINT,
            badge="FREE DOWNLOAD",
            note="まずは読み取り専用モニターで、あなたのVR環境を確認してください。",
            image_box=(820, 245, 1840, 819),
        )
    )
    return slides


def build_video(slides: list[Path], output: Path) -> None:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "imageio-ffmpeg is required. Install it with: "
            f"{sys.executable} -m pip install imageio-ffmpeg"
        ) from exc

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    scene_seconds = 4.0
    transition = 0.45
    command: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
    for slide in slides:
        command.extend(["-loop", "1", "-t", str(scene_seconds), "-i", str(slide)])
    command.extend(["-f", "lavfi", "-t", str(scene_seconds * len(slides)), "-i", "anullsrc=r=48000:cl=stereo"])

    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(slides)):
        label = f"v{index}"
        filters.append(
            f"[{index}:v]scale=1920:1080,format=yuv420p,"
            f"fade=t=in:st=0:d={transition},"
            f"fade=t=out:st={scene_seconds - transition}:d={transition},"
            f"setsar=1[{label}]"
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
            "20",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ]
    )
    subprocess.run(command, check=True)


def create_contact_sheet(paths: list[Path], output: Path) -> None:
    thumb_size = (420, 420)
    canvas = gradient((1320, 910)).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    draw.text((34, 25), "FramePilot VR  ·  BOOTH 日本語素材一覧", font=font(38), fill=INK)
    for index, path in enumerate(paths):
        image = Image.open(path).convert("RGB")
        thumb = fit_cover(image, thumb_size)
        x = 30 + (index % 3) * 430
        y = 96 + (index // 3) * 430
        rounded_paste(canvas, thumb, (x, y, x + 400, y + 400), radius=18, shadow=8)
        draw.rounded_rectangle((x + 14, y + 14, x + 68, y + 54), radius=18, fill="#07111CDD")
        draw.text((x + 28, y + 18), str(index), font=font(20), fill=MINT)
    canvas.convert("RGB").save(output, quality=92)


def write_manifest() -> None:
    manifest = PACK / "MANIFEST_SHA256.txt"
    entries: list[str] = []
    for path in sorted(PACK.rglob("*")):
        if not path.is_file() or path == manifest:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {path.relative_to(PACK).as_posix()}")
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the FramePilot VR Japanese BOOTH material pack")
    parser.add_argument(
        "--vr-background",
        type=Path,
        required=True,
        help="ImageGen VR background plate",
    )
    parser.add_argument("--skip-video", action="store_true")
    args = parser.parse_args()

    for directory in (IMAGES, VIDEO, CAPTURES, VIDEO_SLIDES):
        directory.mkdir(parents=True, exist_ok=True)

    icon = Image.open(ROOT / "assets" / "framepilot-vr-icon.png").convert("RGBA")
    app_ja = safe_app_capture(ROOT / "screenshots" / "framepilot-v0.11.0-ja-marketing.png")
    app_en = safe_app_capture(ROOT / "screenshots" / "framepilot-v0.11.0-en-marketing.png")
    app_de = safe_app_capture(ROOT / "screenshots" / "framepilot-v0.11.0-de-marketing.png")
    website = Image.open(ROOT / "screenshots" / "world-bench-live.png").convert("RGB")
    vr_bg = Image.open(args.vr_background).convert("RGB")
    osd = render_osd_japanese(CAPTURES / "osd-ja-actual-render.png")

    app_ja.save(CAPTURES / "app-ja-safe-crop.png")
    app_en.save(CAPTURES / "app-en-safe-crop.png")
    app_de.save(CAPTURES / "app-de-safe-crop.png")
    website.save(CAPTURES / "world-bench-live.png")
    shutil.copy2(args.vr_background, CAPTURES / "imagegen-vr-background.png")

    squares = [
        save_square_cover(icon, app_ja),
        save_square_osd(icon, osd, vr_bg),
        save_square_auto(icon, app_ja),
        save_square_telemetry(icon, website),
        save_square_fullframe(icon, app_ja),
        save_square_languages(icon, app_ja, app_en, app_de),
    ]
    create_contact_sheet(squares, IMAGES / "preview_contact_sheet.png")

    languages_square = Image.open(IMAGES / "05_multilanguage_square_1200.png").convert("RGB")
    slides = save_video_slides(icon, app_ja, website, osd, vr_bg, languages_square)
    if not args.skip_video:
        build_video(slides, VIDEO / "FramePilotVR_PV_JA_1080p.mp4")
    write_manifest()

    print(f"Built {len(squares)} BOOTH images and {len(slides)} video slides in {PACK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
