from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path
import sys
import time

from steamvr_core import (
    APP_NAME,
    AdaptiveRuntime,
    RuntimeConfig,
    StrategyStore,
    ensure_utf8_console,
    executable_dir,
    process_running,
    run_self_test,
)


ensure_utf8_console()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="读取 SteamVR 帧时序并评估/调整按应用渲染分辨率。")
    p.add_argument("--apply", action="store_true", help="允许写入 SteamVR resolutionScale；默认只读。")
    p.add_argument(
        "--continuous-apply",
        action="store_true",
        help="允许单次运行多次调节；未指定时 --apply 最多只写一次。",
    )
    p.add_argument("--min-scale", type=int, default=40)
    p.add_argument("--max-scale", type=int, default=150)
    p.add_argument("--step-down", type=int, default=1)
    p.add_argument("--step-up", type=int, default=5)
    p.add_argument(
        "--target-fps",
        type=float,
        default=0.0,
        help="高级兼容项：绝对帧预算目标，例如 60 对应 16.67ms；会覆盖 --target-divisor",
    )
    p.add_argument(
        "--target-divisor",
        type=int,
        choices=(1, 2, 3, 4),
        default=1,
        help="按头显刷新率设置节拍：1=原生，2=1/2，3=1/3，4=1/4",
    )
    p.add_argument("--window-seconds", type=float, default=3.0)
    p.add_argument("--evaluate-seconds", type=float, default=0.25)
    p.add_argument("--cooldown-seconds", type=float, default=8.0)
    p.add_argument("--raise-stable-seconds", type=float, default=12.0)
    p.add_argument("--duration", type=float, default=0.0, help="运行秒数；0 表示直到 Ctrl+C。")
    p.add_argument("--wait", action="store_true", help="SteamVR 未运行时等待，而不是直接退出。")
    p.add_argument("--restore-on-exit", action="store_true", help="退出时恢复本次运行首次观察到的设置。")
    p.add_argument("--log-dir", type=Path, default=executable_dir() / "logs")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--import-policy", type=Path, help="导入便携策略；为安全起见会强制只读")
    p.add_argument("--export-policy", type=Path, help="导出当前阈值/步长，不含本机分辨率校准")
    return p


def build_config(args: argparse.Namespace) -> RuntimeConfig:
    if args.continuous_apply and not args.apply:
        raise ValueError("--continuous-apply 必须与 --apply 一起使用")
    mode = "continuous" if args.continuous_apply else "one_step" if args.apply else "monitor"
    return RuntimeConfig(
        mode=mode,
        armed=args.apply,
        target_divisor=0 if args.target_fps > 0 else args.target_divisor,
        target_fps=args.target_fps,
        min_scale=args.min_scale,
        max_scale=args.max_scale,
        step_down=args.step_down,
        step_up=args.step_up,
        window_seconds=args.window_seconds,
        evaluate_seconds=args.evaluate_seconds,
        cooldown_seconds=args.cooldown_seconds,
        raise_stable_seconds=args.raise_stable_seconds,
        restore_on_exit=args.restore_on_exit,
    ).validated()


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        return run_self_test()
    try:
        config = build_config(args)
        if args.import_policy:
            config = StrategyStore.import_portable(args.import_policy, config)
    except (OSError, ValueError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    if args.export_policy:
        try:
            StrategyStore.export_portable(args.export_policy, config, args.export_policy.stem)
            print(f"便携策略已导出: {args.export_policy}")
            return 0
        except (OSError, ValueError) as exc:
            print(f"导出策略失败: {exc}", file=sys.stderr)
            return 2

    print(APP_NAME + " CLI PoC")
    mode_names = {"monitor": "只读监控", "one_step": "单步写入", "continuous": "连续写入"}
    print("模式:", mode_names[config.mode])
    target_text = (
        f"头显刷新率的 1/{config.target_divisor}"
        if config.target_divisor > 1
        else "头显原生刷新率"
        if config.target_divisor == 1
        else f"自定义 {config.target_fps:g} FPS"
    )
    print("目标帧率预算:", target_text)
    if config.armed:
        print("警告: 已解锁 SteamVR resolutionScale 写入。")

    while not process_running("vrserver.exe"):
        if not args.wait:
            print("SteamVR 未运行。请先启动 PICO Connect 和 SteamVR，或添加 --wait。")
            return 3
        print("等待 SteamVR...", flush=True)
        time.sleep(2.0)

    runtime = AdaptiveRuntime(config)
    log_file = None
    writer = None
    started = time.monotonic()
    try:
        args.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = args.log_dir / f"steamvr_adaptive_{stamp}.csv"
        log_file = log_path.open("w", newline="", encoding="utf-8-sig")
        print(f"日志: {log_path}")

        while args.duration <= 0 or time.monotonic() - started < args.duration:
            snapshot = runtime.poll()
            for level, message in runtime.drain_events():
                print(f"{level.upper()}: {message}", flush=True)
            if snapshot is not None:
                data = snapshot.as_dict()
                if writer is None:
                    writer = csv.DictWriter(log_file, fieldnames=list(data.keys()))
                    writer.writeheader()
                writer.writerow(data)
                log_file.flush()
                gpu_text = "n/a" if snapshot.system_gpu_pct is None else f"{snapshot.system_gpu_pct:.0f}%"
                tag = "APPLIED" if snapshot.write_applied else "suggest" if snapshot.decision.action != "hold" else "hold"
                print(
                    f"[{snapshot.local_time:%H:%M:%S}] {snapshot.app_key} | scale={snapshot.resolution_scale}% | "
                    f"GPU p95={snapshot.gpu_p95_ms:.2f}/{snapshot.budget_ms:.2f}ms "
                    f"CPU p95={snapshot.cpu_p95_ms:.2f}ms | reproj={snapshot.reprojection_pct:.1f}% "
                    f"drop={snapshot.dropped} | sys={snapshot.system_cpu_pct:.0f}%/{gpu_text} | "
                    f"{tag}: {snapshot.decision.proposed_scale}% {snapshot.decision.reason}",
                    flush=True,
                )
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("已停止。")
    except Exception as exc:
        print(f"运行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    finally:
        runtime.close()
        if log_file is not None:
            log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
