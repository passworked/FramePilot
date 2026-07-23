from __future__ import annotations

import argparse
import ctypes
import json
import math
from pathlib import Path
import socket
import sys
import time

import openvr
import psutil
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QOffscreenSurface,
    QOpenGLContext,
    QPainter,
    QPen,
    QSurfaceFormat,
)
from PySide6.QtOpenGL import QOpenGLTexture


OVERLAY_HOST = "127.0.0.1"
OVERLAY_PORT = 39421
OVERLAY_KEY = "codex.steamvr.adaptive-resolution.osd"
OVERLAY_NAME = "FramePilot VR OSD"
DEFAULT_FIELDS = (
    "fps",
    "gpu_ms",
    "cpu_ms",
    "gpu_util",
    "budget",
    "resolution",
    "scale",
    "decision",
    "vrc_context",
)
DEFAULT_CONFIG = {"anchor": "upper_left", "size_pct": 100}
STALE_TELEMETRY_SECONDS = 2.5
MIN_TEXTURE_UPDATE_SECONDS = 0.5
OSD_WIDTH = 720
OSD_ROW_HEIGHT = 46
# Keep the GPU texture dimensions stable from the waiting screen onward.
# SteamVR may cache the dimensions of an already-bound OpenGL texture.
OSD_MAX_ROWS = 12
OSD_HEIGHT = 22 + OSD_ROW_HEIGHT * OSD_MAX_ROWS + 18


class StatusReporter:
    def __init__(self, status_path: Path | None = None) -> None:
        self.last: tuple[str, str] | None = None
        self.status_path = status_path

    def emit(self, state: str, detail: str = "") -> None:
        current = (state, detail)
        if current == self.last:
            return
        self.last = current
        payload = json.dumps({"state": state, "detail": detail}, ensure_ascii=True)
        if self.status_path is not None:
            try:
                self.status_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.status_path.with_suffix(".tmp")
                temporary.write_text(payload, encoding="ascii")
                temporary.replace(self.status_path)
            except OSError:
                pass
        # ASCII JSON keeps QProcess status decoding stable on Windows even when
        # the child inherits a legacy console code page.
        if sys.stdout is not None:
            print(payload, flush=True)


def _steamvr_running() -> bool:
    try:
        return any(proc.info.get("name", "").lower() == "vrserver.exe" for proc in psutil.process_iter(["name"]))
    except (psutil.Error, OSError):
        return False


def _color_for_load(value: float, budget: float) -> QColor:
    ratio = value / max(budget, 0.1)
    if ratio >= 0.92:
        return QColor("#FF5F6D")
    if ratio >= 0.75:
        return QColor("#FFD166")
    return QColor("#63E6BE")


def overlay_rows(data: dict[str, object], visible_fields: list[str]) -> list[tuple[str, str, QColor]]:
    gpu = float(data.get("gpu_p95_ms", 0.0))
    cpu = float(data.get("cpu_p95_ms", 0.0))
    budget = float(data.get("budget_ms", 0.0))
    scale = int(data.get("resolution_scale", 100))
    proposed = int(data.get("proposed_scale", scale))
    base_width = int(data.get("render_width", 0))
    base_height = int(data.get("render_height", 0))
    dimension_ratio = math.sqrt(max(scale, 1) / 100.0)
    equivalent_width = round(base_width * dimension_ratio)
    equivalent_height = round(base_height * dimension_ratio)
    system_gpu = data.get("system_gpu_pct")
    system_cpu = float(data.get("system_cpu_pct", 0.0))
    target_fps = float(data.get("target_fps", 0.0))
    frame_interval = float(data.get("frame_interval_p95_ms", 0.0))
    delivered_fps = 1000.0 / frame_interval if frame_interval > 0.0 else 0.0
    reprojection = float(data.get("reprojection_pct", 0.0))
    action = str(data.get("decision", "hold")).upper()
    vrc_world = str(data.get("vrc_world_short", ""))
    vrc_population = int(data.get("vrc_population", 0))
    vrc_ready = bool(data.get("vrc_context_ready", False))
    vrc_safe_scale = int(data.get("vrc_profile_safe_scale", 0))
    if not vrc_world:
        vrc_context_text = "NOT IN VRCHAT"
    elif not vrc_ready:
        vrc_context_text = f"{vrc_world}  /  COUNTING {vrc_population}"
    else:
        learned = f"  /  LEARNED {vrc_safe_scale}%" if vrc_safe_scale > 0 else ""
        vrc_context_text = f"{vrc_world}  /  {vrc_population} PLAYERS{learned}"

    definitions = {
        "fps": ("FRAMERATE", f"{delivered_fps:.1f} FPS", _color_for_load(frame_interval, budget)),
        "gpu_ms": ("GPU P95", f"{gpu:.2f} ms", _color_for_load(gpu, budget)),
        "cpu_ms": ("CPU P95", f"{cpu:.2f} ms", QColor("#FF9D66")),
        "gpu_util": (
            "GPU LOAD",
            "n/a" if system_gpu is None else f"{float(system_gpu):.0f}%",
            QColor("#42D7FF"),
        ),
        "cpu_util": ("CPU LOAD", f"{system_cpu:.0f}%", QColor("#B8C4D1")),
        "budget": ("BUDGET", f"{budget:.2f} ms  /  {target_fps:g} FPS", QColor("#FFD166")),
        "resolution": (
            "RESOLUTION",
            f"{equivalent_width} x {equivalent_height}",
            QColor("#F4F7FB"),
        ),
        "scale": ("STEAMVR", f"{scale}%", QColor("#42D7FF")),
        "decision": (
            "SCHEDULER",
            f"{action}  {scale}% > {proposed}%",
            QColor("#63E6BE") if action == "UP" else QColor("#FF9D66") if action == "DOWN" else QColor("#B8C4D1"),
        ),
        "reprojection": (
            "REPROJECTION",
            f"{reprojection:.1f}%",
            QColor("#FF5F6D") if reprojection >= 3.0 else QColor("#63E6BE"),
        ),
        "vrc_context": (
            "VRC CONTEXT",
            vrc_context_text,
            QColor("#B89CFF") if vrc_ready else QColor("#FFD166"),
        ),
    }
    return [definitions[field] for field in visible_fields if field in definitions]


def render_osd(
    data: dict[str, object] | None,
    visible_fields: list[str],
    canvas: QImage | None = None,
) -> QImage:
    if data is None:
        rows = [("STEAMVR", "WAITING FOR TELEMETRY", QColor("#FFD166"))]
    else:
        rows = overlay_rows(data, visible_fields)
        if not rows:
            rows = [("OSD", "NO METRICS SELECTED", QColor("#B8C4D1"))]

    width = OSD_WIDTH
    row_height = OSD_ROW_HEIGHT
    height = OSD_HEIGHT
    if (
        canvas is None
        or canvas.width() != width
        or canvas.height() != height
        or canvas.format() != QImage.Format.Format_RGBA8888_Premultiplied
    ):
        canvas = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
    image = canvas
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(28)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)

    for index, (label, value, value_color) in enumerate(rows):
        top = 18 + index * row_height
        label_rect = QRectF(18, top, 250, row_height)
        value_rect = QRectF(250, top, width - 270, row_height)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            painter.setPen(QPen(QColor(0, 0, 0, 230), 2))
            painter.drawText(label_rect.translated(dx, dy), Qt.AlignmentFlag.AlignVCenter, label)
            painter.drawText(
                value_rect.translated(dx, dy),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                value,
            )
        painter.setPen(QColor("#D5DEE8"))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter, label)
        painter.setPen(value_color)
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value)

    painter.end()
    return image


def _submit_image(overlay: openvr.IVROverlay, handle: int, image: QImage) -> None:
    # Use the QImage storage directly. Creating and copying a fresh ctypes
    # buffer for every sample caused allocator growth and visible texture
    # replacement flashes in SteamVR.
    bits = image.bits()
    buffer = (ctypes.c_ubyte * image.sizeInBytes()).from_buffer(bits)
    overlay.setOverlayRaw(handle, buffer, image.width(), image.height(), 4)


class PersistentTextureUploader:
    """Keeps one OpenGL texture alive so SteamVR never sees a blank swap."""

    def __init__(self) -> None:
        self.surface = QOffscreenSurface()
        self.surface.setFormat(QSurfaceFormat.defaultFormat())
        self.surface.create()
        self.context = QOpenGLContext()
        self.context.setFormat(self.surface.format())
        if not self.context.create() or not self.context.makeCurrent(self.surface):
            raise RuntimeError("无法创建 Overlay OpenGL 上下文")
        self.texture: QOpenGLTexture | None = None
        self.descriptor: openvr.Texture_t | None = None
        self.size = (0, 0)
        self.bound_handle: int | None = None

    def _create_texture(self, image: QImage) -> None:
        if self.texture is not None:
            self.texture.destroy()
        self.texture = QOpenGLTexture(image, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps)
        if not self.texture.isCreated():
            raise RuntimeError("无法创建 Overlay OpenGL 纹理")
        self.texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self.texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self.texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self.descriptor = openvr.Texture_t()
        self.descriptor.handle = ctypes.c_void_p(self.texture.textureId())
        self.descriptor.eType = openvr.TextureType_OpenGL
        self.descriptor.eColorSpace = openvr.ColorSpace_Gamma
        self.size = (image.width(), image.height())

    def submit(self, overlay: openvr.IVROverlay, handle: int, image: QImage) -> None:
        if not self.context.makeCurrent(self.surface):
            raise RuntimeError("Overlay OpenGL 上下文已失效")
        if self.texture is None or self.size != (image.width(), image.height()):
            self._create_texture(image)
        else:
            # Keep the existing storage and only replace its pixels. The
            # QImage overload attempts to redefine storage and can momentarily
            # detach the texture SteamVR is sampling.
            self.texture.setData(
                QOpenGLTexture.PixelFormat.RGBA,
                QOpenGLTexture.PixelType.UInt8,
                image.bits(),
            )
        self.context.functions().glFinish()
        assert self.descriptor is not None
        # Notify SteamVR after every pixel upload. This keeps the same OpenGL
        # texture object/handle, but lets the compositor consume its new
        # contents; binding only once leaves the initial WAITING frame frozen.
        overlay.setOverlayTexture(handle, self.descriptor)
        self.bound_handle = handle

    def close(self) -> None:
        if self.context.makeCurrent(self.surface) and self.texture is not None:
            self.texture.destroy()
        self.texture = None
        self.descriptor = None
        self.bound_handle = None
        self.context.doneCurrent()


def _head_locked_transform(anchor: str = "upper_left") -> openvr.HmdMatrix34_t:
    positions = {
        "upper_left": (-0.48, 0.28),
        "upper_right": (0.48, 0.28),
        "lower_left": (-0.48, -0.28),
        "lower_right": (0.48, -0.28),
    }
    x, y = positions.get(anchor, positions["upper_left"])
    transform = openvr.HmdMatrix34_t()
    transform.m[0][0] = 1.0
    transform.m[1][1] = 1.0
    transform.m[2][2] = 1.0
    transform.m[0][3] = x
    transform.m[1][3] = y
    transform.m[2][3] = -1.25
    return transform


def run_overlay(status_path: Path | None = None) -> int:
    reporter = StatusReporter(status_path)
    reporter.emit("starting")
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        receiver.bind((OVERLAY_HOST, OVERLAY_PORT))
    except OSError as exc:
        reporter.emit("error", f"遥测端口 {OVERLAY_PORT} 被占用: {exc}")
        return 2
    receiver.settimeout(0.2)
    latest: dict[str, object] | None = None
    visible_fields = list(DEFAULT_FIELDS)
    overlay_config = dict(DEFAULT_CONFIG)
    last_telemetry_at = 0.0
    texture_uploader = PersistentTextureUploader()

    while True:
        handle: int | None = None
        overlay: openvr.IVROverlay | None = None
        created = False
        try:
            openvr.init(openvr.VRApplication_Overlay)
            overlay = openvr.VROverlay()
            handle = overlay.createOverlay(OVERLAY_KEY, OVERLAY_NAME)
            texture_uploader.bound_handle = None
            created = True
            overlay.setOverlayWidthInMeters(handle, 0.72 * int(overlay_config["size_pct"]) / 100.0)
            overlay.setOverlayAlpha(handle, 1.0)
            overlay.setOverlayFlag(handle, openvr.VROverlayFlags_IsPremultiplied, True)
            overlay.setOverlayTextureColorSpace(handle, openvr.ColorSpace_Gamma)
            # QImage stores its first scanline at the top while an OpenGL
            # texture's V origin is at the bottom. Flip only V so text is
            # upright in the headset; U remains left-to-right.
            texture_bounds = openvr.VRTextureBounds_t()
            texture_bounds.uMin = 0.0
            texture_bounds.vMin = 1.0
            texture_bounds.uMax = 1.0
            texture_bounds.vMax = 0.0
            overlay.setOverlayTextureBounds(handle, texture_bounds)
            overlay.setOverlayTransformTrackedDeviceRelative(
                handle,
                openvr.k_unTrackedDeviceIndex_Hmd,
                _head_locked_transform(str(overlay_config["anchor"])),
            )
            render_canvas = render_osd(latest, visible_fields)
            texture_uploader.submit(overlay, handle, render_canvas)
            last_submit_at = time.monotonic()
            texture_dirty = False
            overlay.showOverlay(handle)
            reporter.emit("waiting_scene", "Overlay 已创建，等待场景遥测")

            while True:
                try:
                    packet, _address = receiver.recvfrom(65535)
                except socket.timeout:
                    now = time.monotonic()
                    if latest is not None and now - last_telemetry_at >= STALE_TELEMETRY_SECONDS:
                        latest = None
                        render_canvas = render_osd(None, visible_fields, render_canvas)
                        texture_uploader.submit(overlay, handle, render_canvas)
                        last_submit_at = now
                        texture_dirty = False
                        reporter.emit("waiting_scene", "遥测已中断，等待场景应用")
                    elif texture_dirty and now - last_submit_at >= MIN_TEXTURE_UPDATE_SECONDS:
                        render_canvas = render_osd(latest, visible_fields, render_canvas)
                        texture_uploader.submit(overlay, handle, render_canvas)
                        last_submit_at = now
                        texture_dirty = False
                    continue
                message = json.loads(packet.decode("utf-8"))
                if not isinstance(message, dict):
                    continue
                payload = message.get("data")
                fields = message.get("visible_fields")
                config = message.get("config")
                if isinstance(payload, dict):
                    latest = payload
                    last_telemetry_at = time.monotonic()
                    texture_dirty = True
                    reporter.emit("active", str(payload.get("app_key", "")))
                if isinstance(fields, list):
                    new_fields = [str(field) for field in fields]
                    if new_fields != visible_fields:
                        visible_fields = new_fields
                        texture_dirty = True
                if isinstance(config, dict):
                    anchor = str(config.get("anchor", overlay_config["anchor"]))
                    if anchor not in {"upper_left", "upper_right", "lower_left", "lower_right"}:
                        anchor = "upper_left"
                    size_pct = max(70, min(140, int(config.get("size_pct", overlay_config["size_pct"]))))
                    new_config = {"anchor": anchor, "size_pct": size_pct}
                    if new_config != overlay_config:
                        overlay_config = new_config
                        overlay.setOverlayWidthInMeters(handle, 0.72 * size_pct / 100.0)
                        overlay.setOverlayTransformTrackedDeviceRelative(
                            handle,
                            openvr.k_unTrackedDeviceIndex_Hmd,
                            _head_locked_transform(anchor),
                        )
                now = time.monotonic()
                if texture_dirty and now - last_submit_at >= MIN_TEXTURE_UPDATE_SECONDS:
                    render_canvas = render_osd(latest, visible_fields, render_canvas)
                    texture_uploader.submit(overlay, handle, render_canvas)
                    last_submit_at = now
                    texture_dirty = False
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            state = "waiting_steamvr" if not _steamvr_running() else "error"
            reporter.emit(state, str(exc))
            time.sleep(2.0)
        finally:
            try:
                if created and overlay is not None and handle is not None:
                    overlay.destroyOverlay(handle)
            except Exception:
                pass
            try:
                openvr.shutdown()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SteamVR compact telemetry overlay")
    parser.add_argument("--preview", type=Path, help="Render a static PNG without SteamVR")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QGuiApplication([])
    if args.preview:
        sample = {
            "gpu_p95_ms": 20.0,
            "cpu_p95_ms": 8.0,
            "budget_ms": 33.333,
            "target_fps": 30.0,
            "frame_interval_p95_ms": 27.0,
            "system_gpu_pct": 98.0,
            "system_cpu_pct": 24.0,
            "render_width": 2000,
            "render_height": 1800,
            "resolution_scale": 150,
            "proposed_scale": 149,
            "decision": "down",
            "reprojection_pct": 1.2,
            "vrc_world_short": "8be3a78b",
            "vrc_population": 20,
            "vrc_context_ready": True,
            "vrc_profile_safe_scale": 142,
        }
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        return 0 if render_osd(sample, list(DEFAULT_FIELDS)).save(str(args.preview)) else 1
    try:
        return run_overlay()
    except Exception as exc:
        StatusReporter().emit("error", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
