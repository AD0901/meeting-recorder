/*
 * 板级支持：Waveshare ESP32-S3-Touch-LCD-1.54
 * 引脚来源与实测结论见 docs/hardware.md
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_codec_dev.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

#define BOARD_SAMPLE_RATE   16000
#define BOARD_IN_CH         4      /* ES7210 TDM slot 数 */
#define BOARD_MIC1_SLOT     0      /* 实测 ch0 = MIC1 */
#define BOARD_MIC2_SLOT     2      /* 实测 ch2 = MIC2 */
#define BOARD_IN_GAIN_DB    30.0f

#define BOARD_LCD_W         240
#define BOARD_LCD_H         240

/* 按键（低电平有效）。外壳版屏幕朝向自己：左 BOOT / 中 PWR / 右 PLUS */
#define BOARD_PIN_KEY_BOOT  0
#define BOARD_PIN_KEY_PLUS  4
#define BOARD_PIN_KEY_PWR   5

/* 电源保持、I2C、音频编解码、电池 ADC */
void board_init(void);
void board_power_hold(bool on);

esp_codec_dev_handle_t board_mic(void);      /* 4 路 TDM 交错 16bit */
esp_codec_dev_handle_t board_speaker(void);  /* 单声道 16bit */

void board_lcd_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel, size_t max_transfer_bytes);
void board_backlight(uint8_t percent);

int  board_battery_mv(void);       /* 读不到返回 -1 */
int  board_battery_percent(void);  /* 粗略电量 0–100 */
bool board_is_charging(void);
