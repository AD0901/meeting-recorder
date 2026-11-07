# 会议录音器原型（ESP32-S3 → 转写 + 说话人分离 → 命名 → 纪要）

需求见 [PROJECT_BRIEF.md](PROJECT_BRIEF.md)，硬件引脚见 [docs/hardware.md](docs/hardware.md)。

## 目录

| 目录 | 内容 |
|---|---|
| `hw-test/01_factory` | 微雪官方 Touch 版出厂示例（里程碑 1 硬件自检用，未改动） |
| `firmware/` | 本项目固件（ESP-IDF 5.5，C） |
| `server/` | FastAPI 服务 + 命名网页 |

## 环境

- ESP-IDF v5.5.6，安装在 `~/esp/esp-idf`
- 每个新终端先执行：`. ~/esp/esp-idf/export.sh`
- 开发板串口：`/dev/cu.usbmodem101`

> ⚠️ ESP-IDF 不支持带空格的路径。本项目放在 `cc record demo` 下，编译时通过无空格的软链接 `~/esp/mr` 进入：
> `ln -sfn "$HOME/cc record demo" ~/esp/mr`

## 里程碑 1：硬件自检

```bash
. ~/esp/esp-idf/export.sh
cd ~/esp/mr/hw-test/01_factory
idf.py -p /dev/cu.usbmodem101 build flash monitor
```

- 屏幕亮起，显示 LVGL 界面
- **长按 BOOT**：录音 3 秒；**再长按 BOOT**：喇叭回放
- 这块是触摸版，**滑动屏幕**翻页；**长按 PWR**：关机（电池供电时）
- 退出串口监视：`Ctrl+]`

外壳版按键位置（屏幕朝向自己）：**左 = BOOT，中 = PWR，右 = PLUS**。

### 麦克风探测固件（推荐，比官方示例更清楚）

```bash
cd ~/esp/mr/hw-test/mic_probe && idf.py -p /dev/cu.usbmodem101 flash
cd ~/esp/mr && python tools/serial_capture.py
```

- 待机时串口每 0.5 秒打印四路电平
- 短按 BOOT：录 5 秒，四路分别存成 `recordings/probe_*_chN.wav`，然后在喇叭上回放
- 结论见 [docs/hardware.md](docs/hardware.md)：ch0 = MIC1，ch2 = MIC2

✅ 里程碑 1 已于 2026-11-08 验收：屏幕显示正常，录音回放能听到声音。
