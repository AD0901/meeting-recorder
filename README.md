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

- **右键 [+]**：开始 / 停止录音　**左键 BOOT**：录音中打点　**中键 PWR**：电池供电时按一下开机，长按 3 秒关机
- 屏幕：READY → REC（红点、计时、打点数、音量条）→ SENDING → DONE
- 录音保存在 `recordings/session_<时间>_<设备>_s<N>.wav`，同名 `.json` 记录打点时间、缺片情况、电平

固件结构（`firmware/main/`）：

| 文件 | 作用 |
|---|---|
| `board.c` | 引脚、电源保持、I2C、ES7210/ES8311、屏幕、背光、电池 |
| `audio_capture.c` | CPU1 采集任务：MIC1/MIC2 平均成单声道，按 1 秒一片入队；开始 / 打点 / 结束也按时间顺序入队 |
| `chunk_ring.c` | PSRAM 环形缓冲，120 片 ≈ 2 分钟；消费者确认后才出队 |
| `sd_store.c` | TF 卡（里程碑 5 新增）：SDMMC 挂载、写卡任务（ring 消费者）、meta、Save/Discard、启动恢复扫描 |
| `transport.c` | CPU0 传输任务：从卡读已 Save 的录音分片上传（HTTP / USB 串口），断点续传，云端确认后删卡 |
| `ui.c` | LVGL 状态页（READY / REC / CONFIRM / SENDING / DONE / SETUP） |

✅ 里程碑 2 已于 2026-11-08 验收：录 28.9 秒，29/29 片齐全，3 次打点，没有断续。

## 里程碑 3：Wi-Fi 分片上传 + 服务端

```
设备 ──Wi-Fi / 手机热点──▶ 服务端（Mac 或云服务器）──▶ OSS ──▶ 通义听悟 ──▶ 纪要网页
```

### 服务端

```bash
cd server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 120
```

- 会议列表：http://localhost:8000 ；每次录音结束后自动转写，生成的纪要在 `/sessions/<id>`。
- 配置在 `server/.env`（参考 `.env.example`）：
  - `STORAGE`：`local` 或 `oss`，决定录音和纪要长期存在哪里。
  - `ASR_PROVIDER`：`tingwu` 或 `none`，决定用不用转写。
  - `API_TOKEN`：设备上传时要带的口令。
  - `WEB_USER` / `WEB_PASSWORD`：网页登录的账号和密码。
- 部署到云服务器（Ubuntu 22.04，防火墙放行 TCP 8000）：`server/deploy/deploy.sh <公网IP>`。
  - 第一次运行会生成 `server/.env.cloud`，里面有设备口令和网页密码，这个文件不进 git。

### 设备配网

1. 进入配网模式：
   - 第一次开机没有任何配置时，自动进入；
   - 平时可以点待机页的 **Wi-Fi setup**，或者长按左键 3 秒。
2. 手机扫屏幕上的二维码，或者手动连热点 `MeetRec-XXXXXX`，然后在浏览器打开 `192.168.4.1`。
3. 最多填 5 个 Wi-Fi，比如家里、公司、会议室、手机热点。设备只支持 2.4GHz；iPhone 当热点时要打开"最大兼容性"。
4. 填服务器地址：
   - 用 Mac 当服务端：`http://电脑名.local:8000`
   - 用云服务器：`http://公网IP:8000`
5. 口令填 `API_TOKEN` 的值。

不配网络时，设备照旧走 USB 串口，用 `tools/serial_capture.py` 接收。

### 上传可靠性

> ⚠️ 里程碑 5 起改为「先存卡、SAVE 确认后上传」，本节描述同步更新，详见下文里程碑 5。

- 每片音频（1 秒）都要服务端返回 200 才推进断点；失败就退避重试，重试间隔最长 4 秒。
- 断网或服务端暂时连不上时，录音照常写到 TF 卡，恢复后按顺序补传；原 PSRAM 120 秒缓冲保留为采集→写卡的抖动缓冲。
- 只有服务端确认 END、校验分片齐全后才删卡上文件，屏幕显示 **Uploaded**。

✅ 2026-11-08 本地联调：录音 102 秒，102/102 片齐全，2 次打点。录音中途服务端重启约 15 秒，设备自动补传，没有丢片。
   约 20 秒后听悟出了纪要。⚠️ 说话人分成了 5 个，实际只有 2–3 个人，属于过度分离，里程碑 4 的命名页里要支持合并。

✅ 2026-11-19 云端部署：服务端跑在阿里云轻量服务器（Ubuntu 24.04 + systemd），设备在任何能上网的 Wi-Fi 下都能上传，
   不再需要与电脑同网络；网页地址见 `server/.env.cloud` 的 `PUBLIC_URL`（该文件不进 git）。

## 里程碑 5：TF 卡离线缓冲 + 录完确认保存（2026-11-19）

录音流程改为：**录音实时写 TF 卡 → 停止后屏幕确认 → SAVE 才上传 → 云端收齐后删卡上文件**。
测试录音选 DEL 直接在设备上删除，不上传。**从此录音需要插 TF 卡**（FAT32 / exFAT 均可）。

- 确认页：SAVE = 触屏或右键 [+]；DEL = 触屏或左键 BOOT。未 SAVE 的录音绝不自动上传
- 录音中可点 **PAUSE** 暂停：计时冻结、停止写卡，点 **RESUME** 继续；暂停时间段会被剪掉，
  上传音频无缝拼接，打点时间轴与拼接后一致。暂停中也可直接停止进入确认页
- 断网录音：照录到卡上；SAVE 后等网络恢复自动补传，可连续存多段，待机页显示待上传段数
- 上传中可点 **Later** 回待机后台传，不耽误录下一段
- 断点续传：每段的云端 session_id 和已传分片序号记在卡上 meta 里，重启、断电接着传
- 断电保护：写卡每 10 秒 fsync，打点实时写 meta；录音中断电，重启后该段进入待确认，由你决定抢救还是删除
- 卡上文件：`/sdcard/rec/<id>.pcm`（裸 PCM 16kHz/16bit/单声道）+ `<id>.json`（状态、打点、断点）
- 网页端：会议列表每行有「删除」按钮，可清理云端测试录音（转写处理中的会话需等完成再删；
  删除上传中的会话后，设备补传收到 404 会自动放弃并删卡上文件）
- 服务端上传协议不变（原 PROJECT_BRIEF 第 5 节）；为降低原型复杂度，卡上直接存 PCM 而非 Opus

## 可选功能：资料增强纪要（2026-11-19）

会议列表点「资料增强」→ 上传会议相关文档（**Word / PPT / Excel / PDF / HTML / MD / TXT**，可多选，
单文件 ≤ 20MB）→ AI 结合基础纪要、全文转写和文档内容，生成**增强版会议纪要**（网页查看 + 下载 Markdown）。

- **文档不长期存云上**：上传后仅在临时目录解析出文本，**原文件立即删除**，不进 OSS；云端只保留生成的 `enhanced.md`
- 文档解析全部在服务器本地完成（`app/docs_extract.py`），无额外云服务费；扫描件 PDF 暂不支持（会明确报错）
- LLM 配置在 `server/.env`：`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（OpenAI 兼容接口，
  当前用千问AI平台 `qwen3.7-plus`，可换阿里云百炼 `qwen-plus` 或 DeepSeek；未配置 key 时该功能不可用）
- 输出结构：会议概要 / 背景与相关资料要点（标注「据《文档名》」来源）/ 讨论要点 / 决议 / 待办 / 风险与遗留问题
- prompt 约束：以转写为事实主线、禁止编造、打点段落重点关注（`app/llm.py`）
