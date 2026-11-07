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

## 里程碑 2：按键录音 + 屏幕状态 + 串口导出

```bash
cd ~/esp/mr/firmware && idf.py -p /dev/cu.usbmodem101 flash
cd ~/esp/mr && python tools/serial_capture.py      # 保持运行，负责接收
```

- **右键 [+]**：开始 / 停止录音　**左键 BOOT**：录音中打点　**中键 PWR 长按**：关机
- 屏幕：READY → REC（红点、计时、打点数、音量条）→ SENDING → DONE
- 录音保存在 `recordings/session_<时间>_<设备>_s<N>.wav`，同名 `.json` 记录打点时间、缺片情况、电平

固件结构（`firmware/main/`）：

| 文件 | 作用 |
|---|---|
| `board.c` | 引脚、电源保持、I2C、ES7210/ES8311、屏幕、背光、电池 |
| `audio_capture.c` | CPU1 采集任务：MIC1/MIC2 平均成单声道，按 1 秒一片入队；开始 / 打点 / 结束也按时间顺序入队 |
| `chunk_ring.c` | PSRAM 环形缓冲，120 片 ≈ 2 分钟；消费者确认后才出队 |
| `transport_serial.c` | CPU0 传输任务：按协议格式写串口（里程碑 3 换成 HTTP） |
| `ui.c` | LVGL 状态页 |

✅ 里程碑 2 已于 2026-11-08 验收：录 28.9 秒，29/29 片齐全，3 次打点，没有断续。
