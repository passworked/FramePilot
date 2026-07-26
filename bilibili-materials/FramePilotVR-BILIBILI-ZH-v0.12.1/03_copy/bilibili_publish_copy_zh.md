# B站发布文案

## 推荐标题

VR 掉帧别急着降画质：30 秒看懂 SteamVR 动态分辨率

## 备选标题

1. VRChat 卡顿怎么查？先看懂 GPU 帧预算
2. 自动调 SteamVR 分辨率，真的能让 VR 更稳吗？
3. 不只看平均帧：用 P95 找出 VR 卡顿

## 简介

FramePilot VR 是一款面向 Windows / SteamVR 的动态分辨率控制面板。

它读取 GPU、CPU 与 OpenVR 帧时序，按当前头显刷新率计算帧预算，并为当前场景建议或调整 SteamVR `resolutionScale`。

核心特点：

- VR 内 OSD：查看 GPU / CPU P95、帧预算、分辨率与重投影
- 动态分辨率：高负载时逐步降低，有余量时谨慎升高
- 默认只读：只有用户手动授权后才写入
- CPU 瓶颈保护：CPU 受限时不会把降画质当作主要方案
- 简体中文界面

注意：它不是游戏限帧器，也不能保证所有游戏立即重建渲染纹理；实际帧率仍受游戏、SteamVR 调度与重投影影响。

完整使用说明与下载地址：请在发布时替换为实际链接。

BGM：[Chill lofi inspired](https://opengameart.org/content/chill-lofi-inspired)
— omfgdude（[CC0](https://creativecommons.org/publicdomain/zero/1.0/)）

## 标签建议

`VR` `SteamVR` `VRChat` `PCVR` `帧率优化` `显卡` `性能测试` `动态分辨率`

## 动态文案

VR 掉帧时，你会先降画质，还是先看 GPU 帧时间？  
用 30 秒看懂 FramePilot VR 如何根据帧预算调整 SteamVR 分辨率。
