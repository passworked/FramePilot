from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts


OUT = Path(__file__).resolve().parent / "audio" / "tts"
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+18%"

LINES = (
    "VR 掉帧，别急着降画质。先看每一帧的时间预算。",
    "GPU、CPU、帧预算，在 VR 里直接看。",
    "重场景逐步降分辨率，有余量再谨慎升高。",
    "默认只读，授权后才写入；退出时恢复初始值。",
    "不只看平均值，用 P 九十五帧时间捕捉真实卡顿。",
    "FramePilot VR，让每一帧更有把握。说明见简介。",
)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for index, line in enumerate(LINES, start=1):
        output = OUT / f"{index:02d}.mp3"
        temporary = output.with_suffix(".tmp.mp3")
        for attempt in range(1, 5):
            try:
                await edge_tts.Communicate(line, VOICE, rate=RATE).save(str(temporary))
                temporary.replace(output)
                break
            except Exception:
                temporary.unlink(missing_ok=True)
                if attempt == 4:
                    raise
                await asyncio.sleep(attempt * 1.5)
        print(f"{output.name}: {line}")


if __name__ == "__main__":
    asyncio.run(main())
