/*
 * mic_probe — 麦克风探测固件（里程碑 1/2 的诊断工具）
 *
 * 待机：每 0.5 秒打印一次 ES7210 四路 TDM 通道的电平（dBFS），用来判断哪一路是 MIC1 / MIC2。
 * 按一下 BOOT（屏幕朝向自己时最左边的键）：
 *   1. 录 5 秒，四路全部保存在 PSRAM
 *   2. 打印每一路的 RMS / 峰值
 *   3. 用 base64 把原始 PCM 发到串口（电脑端 tools/serial_capture.py 接收后存成 WAV）
 *   4. 把最响的那一路在板载喇叭上回放
 *
 * 引脚见 docs/hardware.md（来源：微雪官方 Touch 版 01_factory 示例）。
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/i2s_tdm.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

static const char *TAG = "mic_probe";

/* ---- 引脚 ---- */
#define PIN_I2C_SDA     42
#define PIN_I2C_SCL     41
#define PIN_I2S_MCLK    8
#define PIN_I2S_BCLK    9
#define PIN_I2S_WS      10
#define PIN_I2S_DOUT    12
#define PIN_I2S_DIN     11
#define PIN_PA_EN       7
#define PIN_PWR_HOLD    2   /* 电池供电时必须拉高，否则掉电 */
#define PIN_LCD_BL      46
#define PIN_KEY_BOOT    0

/* ---- 音频参数 ---- */
#define SAMPLE_RATE     16000
#define IN_CH           4                       /* ES7210 TDM 4 slot */
#define FRAME_MS        20
#define FRAME_SAMPLES   (SAMPLE_RATE * FRAME_MS / 1000)          /* 320 */
#define FRAME_BYTES     (FRAME_SAMPLES * IN_CH * sizeof(int16_t)) /* 2560 */
#define REC_SECONDS     5
#define REC_BYTES       (SAMPLE_RATE * REC_SECONDS * IN_CH * sizeof(int16_t))
#define IN_GAIN_DB      30.0f
#define OUT_VOLUME      85

static esp_codec_dev_handle_t s_in_dev;
static esp_codec_dev_handle_t s_out_dev;

static void board_power_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_PWR_HOLD) | (1ULL << PIN_LCD_BL),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io);
    gpio_set_level(PIN_PWR_HOLD, 1);
    gpio_set_level(PIN_LCD_BL, 0);  /* 本固件不用屏幕，关背光 */

    gpio_config_t key = {
        .pin_bit_mask = 1ULL << PIN_KEY_BOOT,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&key);
}

static void audio_init(void)
{
    /* I2C */
    i2c_master_bus_handle_t bus;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &bus));

    /* I2S 全双工：TX = STD 立体声 → ES8311，RX = TDM 4 slot ← ES7210（与官方示例一致） */
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240;
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx, &rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = SAMPLE_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK, .bclk = PIN_I2S_BCLK, .ws = PIN_I2S_WS,
            .dout = PIN_I2S_DOUT, .din = I2S_GPIO_UNUSED,
        },
    };
    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg = {
            .sample_rate_hz = SAMPLE_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
            .bclk_div = 8,
        },
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO,
                        I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK, .bclk = PIN_I2S_BCLK, .ws = PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED, .din = PIN_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_tdm_mode(rx, &tdm_cfg));

    /* esp_codec_dev */
    audio_codec_i2s_cfg_t i2s_if_cfg = { .port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    audio_codec_i2c_cfg_t i2c_cfg = { .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = bus };
    const audio_codec_ctrl_if_t *out_ctrl = audio_codec_new_i2c_ctrl(&i2c_cfg);
    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = out_ctrl,
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = PIN_PA_EN,
        .use_mclk = true,
        .hw_gain = { .pa_voltage = 5.0, .codec_dac_voltage = 3.3 },
    };
    esp_codec_dev_cfg_t out_dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = data_if,
    };
    s_out_dev = esp_codec_dev_new(&out_dev_cfg);
    assert(s_out_dev);

    i2c_cfg.addr = ES7210_CODEC_DEFAULT_ADDR;
    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg),
        .mic_selected = ES7120_SEL_MIC1 | ES7120_SEL_MIC2 | ES7120_SEL_MIC3 | ES7120_SEL_MIC4,
    };
    esp_codec_dev_cfg_t in_dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = es7210_codec_new(&es7210_cfg),
        .data_if = data_if,
    };
    s_in_dev = esp_codec_dev_new(&in_dev_cfg);
    assert(s_in_dev);

    esp_codec_dev_sample_info_t out_fs = { .bits_per_sample = 16, .channel = 1, .sample_rate = SAMPLE_RATE };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_out_dev, &out_fs));
    ESP_ERROR_CHECK(esp_codec_dev_set_out_vol(s_out_dev, OUT_VOLUME));

    /* channel_mask = 0 → 四路全部交错返回：s0 s1 s2 s3 s0 s1 ... */
    esp_codec_dev_sample_info_t in_fs = { .bits_per_sample = 16, .channel = IN_CH, .channel_mask = 0, .sample_rate = SAMPLE_RATE };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_in_dev, &in_fs));
    ESP_ERROR_CHECK(esp_codec_dev_set_in_gain(s_in_dev, IN_GAIN_DB));

    ESP_LOGI(TAG, "audio ready: in %d ch TDM @%dHz gain %.1fdB, out vol %d",
             IN_CH, SAMPLE_RATE, IN_GAIN_DB, OUT_VOLUME);
}

static float to_dbfs(double rms)
{
    return rms < 1.0 ? -90.0f : (float)(20.0 * log10(rms / 32768.0));
}

/* 统计 buf 中每一路的 RMS 与峰值 */
static void channel_stats(const int16_t *buf, size_t frames, double rms[IN_CH], int peak[IN_CH])
{
    double sq[IN_CH] = {0};
    for (int c = 0; c < IN_CH; c++) peak[c] = 0;
    for (size_t i = 0; i < frames; i++) {
        for (int c = 0; c < IN_CH; c++) {
            int v = buf[i * IN_CH + c];
            sq[c] += (double)v * v;
            int a = v < 0 ? -v : v;
            if (a > peak[c]) peak[c] = a;
        }
    }
    for (int c = 0; c < IN_CH; c++) rms[c] = sqrt(sq[c] / (frames ? frames : 1));
}

static void dump_base64(const uint8_t *data, size_t len)
{
    uint32_t crc = esp_rom_crc32_le(0, data, len);
    printf("\n===PCM BEGIN rate=%d ch=%d bits=16 bytes=%u crc32=%08lx===\n",
           SAMPLE_RATE, IN_CH, (unsigned)len, (unsigned long)crc);
    static unsigned char line[1100];
    const size_t step = 768; /* → 1024 个 base64 字符 */
    for (size_t off = 0; off < len; off += step) {
        size_t n = len - off < step ? len - off : step;
        size_t olen = 0;
        mbedtls_base64_encode(line, sizeof(line) - 2, &olen, data + off, n);
        line[olen++] = '\n';
        fwrite(line, 1, olen, stdout);
    }
    printf("===PCM END===\n");
    fflush(stdout);
}

static void record_and_report(int16_t *rec)
{
    ESP_LOGI(TAG, ">>> 开始录音 %d 秒，请对着板子说话", REC_SECONDS);
    size_t got = 0;
    while (got < REC_BYTES) {
        size_t n = REC_BYTES - got < FRAME_BYTES ? REC_BYTES - got : FRAME_BYTES;
        if (esp_codec_dev_read(s_in_dev, (uint8_t *)rec + got, n) != ESP_CODEC_DEV_OK) {
            ESP_LOGE(TAG, "read error");
            break;
        }
        got += n;
    }
    size_t frames = got / (IN_CH * sizeof(int16_t));

    double rms[IN_CH];
    int peak[IN_CH];
    channel_stats(rec, frames, rms, peak);
    int loudest = 0;
    for (int c = 0; c < IN_CH; c++) {
        ESP_LOGI(TAG, "REC ch%d  RMS %6.1f dBFS  peak %5d", c, to_dbfs(rms[c]), peak[c]);
        if (rms[c] > rms[loudest]) loudest = c;
    }

    dump_base64((const uint8_t *)rec, got);

    /* 取最响的一路回放 */
    ESP_LOGI(TAG, ">>> 回放 ch%d", loudest);
    int16_t mono[FRAME_SAMPLES];
    for (size_t i = 0; i < frames; i += FRAME_SAMPLES) {
        size_t n = frames - i < FRAME_SAMPLES ? frames - i : FRAME_SAMPLES;
        for (size_t k = 0; k < n; k++) mono[k] = rec[(i + k) * IN_CH + loudest];
        esp_codec_dev_write(s_out_dev, mono, n * sizeof(int16_t));
    }
    ESP_LOGI(TAG, ">>> 回放结束，可以再按 BOOT 重录");
}

void app_main(void)
{
    board_power_init();
    audio_init();

    int16_t *rec = heap_caps_malloc(REC_BYTES, MALLOC_CAP_SPIRAM);
    static int16_t frame[FRAME_SAMPLES * IN_CH];
    assert(rec);

    ESP_LOGI(TAG, "就绪。待机时每 0.5 秒打印四路电平；按 BOOT 录 %d 秒", REC_SECONDS);

    double acc_sq[IN_CH] = {0};
    int acc_peak[IN_CH] = {0};
    size_t acc_frames = 0;
    int key_low_ms = 0;

    while (1) {
        if (esp_codec_dev_read(s_in_dev, frame, FRAME_BYTES) != ESP_CODEC_DEV_OK) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        double rms[IN_CH];
        int peak[IN_CH];
        channel_stats(frame, FRAME_SAMPLES, rms, peak);
        for (int c = 0; c < IN_CH; c++) {
            acc_sq[c] += rms[c] * rms[c] * FRAME_SAMPLES;
            if (peak[c] > acc_peak[c]) acc_peak[c] = peak[c];
        }
        acc_frames += FRAME_SAMPLES;

        if (acc_frames >= SAMPLE_RATE / 2) {
            printf("LVL dBFS");
            for (int c = 0; c < IN_CH; c++) {
                printf("  ch%d %6.1f (pk %5d)", c, to_dbfs(sqrt(acc_sq[c] / acc_frames)), acc_peak[c]);
                acc_sq[c] = 0;
                acc_peak[c] = 0;
            }
            printf("\n");
            acc_frames = 0;
        }

        /* BOOT 键：低电平有效，按住 >= 60ms 触发 */
        if (gpio_get_level(PIN_KEY_BOOT) == 0) {
            key_low_ms += FRAME_MS;
            if (key_low_ms == 60) {
                record_and_report(rec);
                while (gpio_get_level(PIN_KEY_BOOT) == 0) vTaskDelay(pdMS_TO_TICKS(20));
                acc_frames = 0;
            }
        } else {
            key_low_ms = 0;
        }
    }
}
