# 硬件引脚（Waveshare ESP32-S3-Touch-LCD-1.54）

来源：官方仓库 `waveshareteam/ESP32-S3-Touch-LCD-1.54` 的
`examples/ESP32-S3-Touch-LCD-1.54-demo/ESP-IDF-5.5.1/01_factory/components/esp_bsp/`。
Wiki 上有些页面用的是其他板型的引脚，**以本表为准**。

| 模块 | 引脚 / 参数 |
|---|---|
| I2C（ES8311、ES7210、CST816、QMI8658@0x6B 共用） | SDA 42，SCL 41，I2C0 |
| I2S | MCLK 8，BCLK 9，WS 10，DOUT 12（→ES8311），DIN 11（←ES7210） |
| ES7210 采集 | TDM 4 slot，16kHz/16bit；官方示例只取 channel 0，增益 30dB |
| ES8311 播放 | STD 立体声 16kHz |
| 功放 NS4150B 使能 | GPIO 7 |
| LCD ST7789（SPI2，40MHz） | MOSI 39，SCLK 38，CS 21，DC 45，RST 40，背光 46（LEDC PWM），需 `invert_color=true`，240×240 |
| 触摸 CST816 | INT 48，RST 47 |
| 按键（低电平有效） | BOOT = GPIO 0，PLUS = GPIO 4，PWR = GPIO 5 |
| 电池 | ADC = GPIO 1，充电检测 = GPIO 3（低 = 充电中） |
| **电源保持** | **GPIO 2：开机后必须拉高，否则只用电池供电时会掉电**；拉低即关机 |
| SD 卡（SDMMC 4 线） | CLK 16，CMD 15，D0 17，D1 18，D2 13，D3 14 |

## ES7210 通道实测（2026-11-08，`hw-test/mic_probe`）

用 `esp_codec_dev` 打开输入，`channel=4, channel_mask=0`，四路交错返回：

| 通道 | 内容 | 安静时 | 正常说话（约 30cm，增益 30dB） |
|---|---|---|---|
| ch0 | **MIC1** | −66 dBFS | RMS −33 ~ −40 dBFS，峰值约 27000，不削波 |
| ch1 | 空 | −88 dBFS | — |
| ch2 | **MIC2** | −66 dBFS | 与 ch0 基本相同 |
| ch3 | 空 / AEC 参考 | −87 dBFS | — |

- 两个麦克风都在机壳左侧，间距很近，相关性 0.99，延迟为 0 个采样点：没法做空间定位。
- **单声道方案：取 (ch0 + ch2) / 2。** 语音成分相同，非相关底噪可以降约 3dB。
- 输入增益 30dB 合适；官方示例里的 `set_in_gain(70)` 超出 ES7210 的上限（37.5dB），不要照抄。
- 喇叭输出：`esp_codec_dev_set_out_vol(85)` 能正常回放；官方示例来回把音量设成 0 和 100，只听得到滋滋声。
