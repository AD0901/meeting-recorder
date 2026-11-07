/*
 * 屏幕状态页（LVGL 8）。每 100ms 读取采集 / 传输状态并刷新，不需要其他模块调用。
 * 目前只用内置 Montserrat 字体，所以界面是英文；中文字库以后再加。
 */
#pragma once

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

void ui_init(esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel);
void ui_show_power_off(void);
