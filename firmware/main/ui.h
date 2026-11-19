/*
 * 屏幕界面（LVGL 8）：触摸和侧键都能开始 / 停止 / 打点。
 * 目前只用内置 Montserrat 字体，所以界面是英文；中文字库以后再加。
 */
#pragma once

#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

void ui_init(esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel);
void ui_show_power_off(void);
void ui_request_setup(void);  /* 显示提示后重启进入配网模式 */
void ui_net_ready(void);     /* 网络模块启动后调用（配网模式要画二维码） */

/* 录完确认（侧键 PLUS=Save / BOOT=Discard；实际执行在 LVGL 任务里） */
void ui_confirm_save(void);
void ui_confirm_discard(void);
