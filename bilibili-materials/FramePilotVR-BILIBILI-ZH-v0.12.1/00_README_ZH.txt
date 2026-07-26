FramePilot VR · B站中文视频素材包 · v0.12.1

交付内容
========

01_images/
  FramePilotVR_Bilibili_Cover_16x9.png / .jpg
    1920×1080，16:9 横版封面。

  FramePilotVR_Bilibili_Cover_4x3.png / .jpg
    1600×1200，4:3 封面。

  preview_contact_sheet.jpg
    两张封面与六个视频段落的快速预览。

02_video/
  FramePilotVR_Bilibili_ZH_30s_1080p.mp4
    1920×1080、30 fps、H.264 + AAC，时长 30 秒。
    内含普通话 TTS、CC0 lo-fi BGM，以及白字黑描边的内嵌字幕。

  FramePilotVR_Bilibili_ZH_30s_subtitles.srt
    可上传到 B 站的无障碍字幕。

03_copy/
  bilibili_publish_copy_zh.md
    标题、简介、动态文案与标签建议。

  video_script_zh.md
    分镜、屏幕文案与可选口播稿。

04_source/
  build_bilibili_pack.py
    可重复构建素材的 Python 脚本。

  generate_tts.py
    使用普通话神经语音重新生成六段 TTS。

  audio/
    CC0 lo-fi 原曲、许可记录和成片使用的 TTS 分段。

说明
====

- 视频中的应用界面和 VR OSD 均由 FramePilot VR v0.12.1 实际代码渲染。
- 氛围背景由 OpenAI 内置图像生成工具创建；界面、品牌和中文文案均在本地合成。
- 背景音乐为 omfgdude 创作的 `Chill lofi inspired`，按 CC0 许可使用；
  来源与许可记录位于 `04_source/audio/LICENSE.md`。
- 普通话 TTS 使用 Microsoft `zh-CN-XiaoxiaoNeural` 生成。
- 视频已经烧录白字黑描边字幕；SRT 继续用于平台字幕与无障碍支持。
- 本次仅新增营销素材，不改变应用能力，因此项目版本保持 v0.12.1。
