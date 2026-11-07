#include "ui.h"

#include <stdio.h>

#include "audio_capture.h"
#include "board.h"
#include "chunk_ring.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "transport_serial.h"

#define COLOR_BG     lv_color_hex(0x101418)
#define COLOR_TEXT   lv_color_hex(0xE8EAED)
#define COLOR_DIM    lv_color_hex(0x8A9199)
#define COLOR_RED    lv_color_hex(0xE53935)
#define COLOR_GREEN  lv_color_hex(0x43A047)
#define COLOR_AMBER  lv_color_hex(0xFFB300)

static lv_obj_t *s_top_left, *s_top_right;
static lv_obj_t *s_state, *s_dot, *s_time, *s_info, *s_level, *s_hint;

static lv_obj_t *mk_label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, color, 0);
    lv_label_set_text(l, "");
    return l;
}

static const char *battery_symbol(int pct)
{
    if (pct < 0)   return "";
    if (pct >= 90) return LV_SYMBOL_BATTERY_FULL;
    if (pct >= 60) return LV_SYMBOL_BATTERY_3;
    if (pct >= 35) return LV_SYMBOL_BATTERY_2;
    if (pct >= 10) return LV_SYMBOL_BATTERY_1;
    return LV_SYMBOL_BATTERY_EMPTY;
}

static void refresh_cb(lv_timer_t *t)
{
    static uint32_t tick;
    tick++;

    capture_status_t cap;
    transport_status_t xp;
    capture_get_status(&cap);
    transport_get_status(&xp);

    /* 顶栏：每 2 秒刷新一次电量 */
    if (tick % 20 == 1) {
        int pct = board_battery_percent();
        bool chg = board_is_charging();
        if (pct >= 0) {
            lv_label_set_text_fmt(s_top_left, "%s %d%%%s", battery_symbol(pct), pct, chg ? " " LV_SYMBOL_CHARGE : "");
        } else {
            lv_label_set_text(s_top_left, LV_SYMBOL_USB);
        }
        lv_label_set_text_fmt(s_top_right, "ID %s", transport_device_id() + 6);
    }

    uint32_t sec = cap.elapsed_ms / 1000;
    lv_label_set_text_fmt(s_time, "%02lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));

    if (cap.recording) {
        lv_label_set_text(s_state, "REC");
        lv_obj_set_style_text_color(s_state, COLOR_RED, 0);
        if (tick % 10 < 6) lv_obj_clear_flag(s_dot, LV_OBJ_FLAG_HIDDEN);
        else               lv_obj_add_flag(s_dot, LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_text_color(s_time, COLOR_TEXT, 0);
        lv_label_set_text_fmt(s_info, LV_SYMBOL_BELL " Marks: %lu", (unsigned long)cap.marks);
        int lvl = (int)cap.level_db;
        lv_bar_set_value(s_level, lvl < -60 ? -60 : lvl, LV_ANIM_OFF);
        lv_obj_clear_flag(s_level, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(s_hint, "[+] stop    [BOOT] mark");
        return;
    }

    lv_obj_add_flag(s_dot, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_level, LV_OBJ_FLAG_HIDDEN);

    if (cap.session == 0) {
        lv_label_set_text(s_state, "READY");
        lv_obj_set_style_text_color(s_state, COLOR_GREEN, 0);
        lv_obj_set_style_text_color(s_time, COLOR_DIM, 0);
        lv_label_set_text(s_info, "");
        lv_label_set_text(s_hint, "Press [+] to record");
    } else if (!xp.session_done || ring_used() > 0) {
        uint32_t total = cap.chunks ? cap.chunks : 1;
        lv_label_set_text(s_state, "SENDING");
        lv_obj_set_style_text_color(s_state, COLOR_AMBER, 0);
        lv_label_set_text_fmt(s_info, LV_SYMBOL_UPLOAD " %lu / %lu s",
                              (unsigned long)xp.sent_chunks, (unsigned long)total);
        lv_label_set_text(s_hint, "Exporting over USB...");
    } else {
        lv_label_set_text(s_state, "DONE");
        lv_obj_set_style_text_color(s_state, COLOR_GREEN, 0);
        lv_obj_set_style_text_color(s_time, COLOR_TEXT, 0);
        if (cap.dropped) {
            lv_label_set_text_fmt(s_info, LV_SYMBOL_WARNING " %lu s lost", (unsigned long)cap.dropped);
        } else {
            lv_label_set_text_fmt(s_info, LV_SYMBOL_OK " %lu chunks, %lu marks",
                                  (unsigned long)xp.sent_chunks, (unsigned long)cap.marks);
        }
        lv_label_set_text(s_hint, "Saved on PC. [+] new rec");
    }
}

static void build_screen(void)
{
    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, COLOR_BG, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    s_top_left = mk_label(scr, &lv_font_montserrat_14, COLOR_DIM);
    lv_obj_align(s_top_left, LV_ALIGN_TOP_LEFT, 10, 8);
    s_top_right = mk_label(scr, &lv_font_montserrat_14, COLOR_DIM);
    lv_obj_align(s_top_right, LV_ALIGN_TOP_RIGHT, -10, 8);

    s_state = mk_label(scr, &lv_font_montserrat_20, COLOR_GREEN);
    lv_obj_align(s_state, LV_ALIGN_TOP_MID, 8, 44);

    s_dot = lv_obj_create(scr);
    lv_obj_set_size(s_dot, 16, 16);
    lv_obj_set_style_radius(s_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_dot, COLOR_RED, 0);
    lv_obj_set_style_border_width(s_dot, 0, 0);
    lv_obj_align_to(s_dot, s_state, LV_ALIGN_OUT_LEFT_MID, -30, 0);
    lv_obj_add_flag(s_dot, LV_OBJ_FLAG_HIDDEN);

    s_time = mk_label(scr, &lv_font_montserrat_48, COLOR_DIM);
    lv_obj_align(s_time, LV_ALIGN_CENTER, 0, -8);
    lv_label_set_text(s_time, "00:00");

    s_info = mk_label(scr, &lv_font_montserrat_20, COLOR_TEXT);
    lv_obj_align(s_info, LV_ALIGN_CENTER, 0, 40);

    s_level = lv_bar_create(scr);
    lv_obj_set_size(s_level, 180, 8);
    lv_bar_set_range(s_level, -60, 0);
    lv_obj_set_style_bg_color(s_level, lv_color_hex(0x2A3138), LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_level, COLOR_GREEN, LV_PART_INDICATOR);
    lv_obj_align(s_level, LV_ALIGN_CENTER, 0, 70);
    lv_obj_add_flag(s_level, LV_OBJ_FLAG_HIDDEN);

    s_hint = mk_label(scr, &lv_font_montserrat_14, COLOR_DIM);
    lv_obj_align(s_hint, LV_ALIGN_BOTTOM_MID, 0, -12);

    lv_timer_create(refresh_cb, 100, NULL);
}

void ui_init(esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t panel)
{
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_affinity = 0;   /* CPU1 留给音频采集 */
    port_cfg.task_priority = 3;
    ESP_ERROR_CHECK(lvgl_port_init(&port_cfg));

    lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io,
        .panel_handle = panel,
        .buffer_size = BOARD_LCD_W * 40,
        .double_buffer = true,
        .hres = BOARD_LCD_W,
        .vres = BOARD_LCD_H,
        .flags = { .buff_dma = true },
    };
    lvgl_port_add_disp(&disp_cfg);

    if (lvgl_port_lock(0)) {
        build_screen();
        lvgl_port_unlock();
    }
}

void ui_show_power_off(void)
{
    if (lvgl_port_lock(100)) {
        lv_obj_clean(lv_scr_act());
        lv_obj_t *l = mk_label(lv_scr_act(), &lv_font_montserrat_20, COLOR_TEXT);
        lv_label_set_text(l, LV_SYMBOL_POWER " Power off");
        lv_obj_center(l);
        lvgl_port_unlock();
    }
}
