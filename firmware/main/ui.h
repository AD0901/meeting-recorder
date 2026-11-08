/*
 * 屏幕界面（LVGL 8）：触摸和侧键都能开始 / 停止 / 打点。
 * 目前只用内置 Montserrat 字体，所以界面是英文；中文字库以后再加。
 */
#pragma once

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

void ui_init(esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel);
void ui_show_power_off(void);
void ui_dismiss_done(void);   /* DONE 页回到 READY（侧键 [+] 也能用） */
