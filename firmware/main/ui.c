/*
 * 屏幕界面（LVGL 8，触摸 + 侧键都能操作）
 *
 *   READY    中间一个红色圆形 REC 按钮，点一下开始录音
 *   REC      计时 / 音量条 / 打点数；底部 MARK（打点）和 STOP（停止）两个按钮
 *   SENDING  正在上传（断网时显示 Waiting for Wi-Fi），显示进度
 *   DONE     绿色对勾 + 本次时长、打点数；点 NEW 回到 READY（HTTP 模式下 = 服务端已确认收齐）
 *   SETUP    配网模式：二维码（手机扫码连设备热点）+ 打开 192.168.4.1 的提示
 *
 * 顶栏右侧是网络状态：绿色 Wi-Fi = 服务端连通；黄色 = 连上 Wi-Fi 但服务端不通；灰色 = 找网中。
 *
 * 只读取采集 / 传输模块的状态，每 100ms 刷新一次。
 */
#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "audio_capture.h"
#include "board.h"
#include "chunk_ring.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "transport.h"
#include "wifi_mgr.h"

#define COLOR_BG     lv_color_hex(0x101418)
#define COLOR_PANEL  lv_color_hex(0x1C2228)
#define COLOR_TEXT   lv_color_hex(0xE8EAED)
#define COLOR_DIM    lv_color_hex(0x8A9199)
#define COLOR_RED    lv_color_hex(0xE53935)
#define COLOR_GREEN  lv_color_hex(0x43A047)
#define COLOR_AMBER  lv_color_hex(0xFFB300)

typedef enum { V_READY, V_REC, V_SENDING, V_DONE, V_SETUP, V_COUNT } view_t;

static lv_obj_t *s_top_left, *s_top_right;
static lv_obj_t *s_view[V_COUNT];
static lv_obj_t *s_ready_status, *s_rec_net, *s_send_title, *s_done_title, *s_setup_ssid, *s_setup_qr;
/* REC */
static lv_obj_t *s_rec_dot, *s_rec_time, *s_rec_marks, *s_rec_level;
/* SENDING */
static lv_obj_t *s_send_bar, *s_send_label;
/* DONE */
static lv_obj_t *s_done_info;

static uint32_t s_dismissed_session;  /* DONE 页点了 NEW 的会话号 */
static uint32_t s_mark_flash;         /* 打点后按钮闪一下 */

/* ---------- 小工具 ---------- */

static lv_obj_t *mk_label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, color, 0);
    lv_label_set_text(l, text);
    return l;
}

static lv_obj_t *mk_view(lv_obj_t *scr)
{
    lv_obj_t *v = lv_obj_create(scr);
    lv_obj_remove_style_all(v);
    lv_obj_set_size(v, BOARD_LCD_W, BOARD_LCD_H - 30);
    lv_obj_align(v, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_clear_flag(v, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(v, LV_OBJ_FLAG_HIDDEN);
    return v;
}

static lv_obj_t *mk_button(lv_obj_t *parent, lv_coord_t w, lv_coord_t h, lv_color_t bg, lv_coord_t radius,
                           const char *text, const lv_font_t *font, lv_event_cb_t cb)
{
    lv_obj_t *b = lv_btn_create(parent);
    lv_obj_set_size(b, w, h);
    lv_obj_set_style_bg_color(b, bg, 0);
    lv_obj_set_style_bg_color(b, lv_color_lighten(bg, LV_OPA_30), LV_STATE_PRESSED);
    lv_obj_set_style_radius(b, radius, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l = mk_label(b, font, lv_color_white(), text);
    lv_obj_center(l);
    return b;
}

static const char *battery_symbol(int pct)
{
    if (pct >= 90) return LV_SYMBOL_BATTERY_FULL;
    if (pct >= 60) return LV_SYMBOL_BATTERY_3;
    if (pct >= 35) return LV_SYMBOL_BATTERY_2;
    if (pct >= 10) return LV_SYMBOL_BATTERY_1;
    return LV_SYMBOL_BATTERY_EMPTY;
}

/* ---------- 触摸事件（在 LVGL 任务里执行） ---------- */

static void on_rec_tap(lv_event_t *e)  { capture_request_start(); }
static void on_stop_tap(lv_event_t *e) { capture_request_stop(); }
static void on_mark_tap(lv_event_t *e) { capture_request_mark(); s_mark_flash = 3; }
static void on_wifi_tap(lv_event_t *e) { ui_request_setup(); }
static void on_new_tap(lv_event_t *e)
{
    capture_status_t cap;
    capture_get_status(&cap);
    s_dismissed_session = cap.session;
}

/* ---------- 刷新 ---------- */

static void show_view(view_t v)
{
    for (int i = 0; i < V_COUNT; i++) {
        if (i == v) lv_obj_clear_flag(s_view[i], LV_OBJ_FLAG_HIDDEN);
        else        lv_obj_add_flag(s_view[i], LV_OBJ_FLAG_HIDDEN);
    }
}

static void refresh_cb(lv_timer_t *t)
{
    static uint32_t tick;
    tick++;

    capture_status_t cap;
    transport_status_t xp;
    capture_get_status(&cap);
    transport_get_status(&xp);
    net_status_t ns;
    net_get_status(&ns);
    bool http = xp.mode == XPORT_HTTP;

    if (tick % 5 == 1) {  /* 网络状态 */
        if (ns.state == NET_SETUP) {
            lv_label_set_text(s_top_right, LV_SYMBOL_SETTINGS " SETUP");
            lv_obj_set_style_text_color(s_top_right, COLOR_AMBER, 0);
        } else if (!http) {
            lv_label_set_text(s_top_right, LV_SYMBOL_USB " USB");
            lv_obj_set_style_text_color(s_top_right, COLOR_DIM, 0);
        } else if (ns.state == NET_CONNECTED) {
            lv_label_set_text_fmt(s_top_right, LV_SYMBOL_WIFI " %s", xp.online ? "OK" : "!");
            lv_obj_set_style_text_color(s_top_right, xp.online ? COLOR_GREEN : COLOR_AMBER, 0);
        } else {
            lv_label_set_text(s_top_right, LV_SYMBOL_WIFI " ...");
            lv_obj_set_style_text_color(s_top_right, COLOR_DIM, 0);
        }
        /* 待机页状态行 */
        if (!http) {
            lv_label_set_text(s_ready_status, "USB mode  ·  hold " LV_SYMBOL_LEFT " 3s for Wi-Fi");
        } else if (ns.state != NET_CONNECTED) {
            lv_label_set_text(s_ready_status, ns.ssid[0] ? "Connecting Wi-Fi ..." : "No known Wi-Fi nearby");
        } else if (xp.auth_error) {
            lv_label_set_text(s_ready_status, "Server: wrong token");
        } else if (!xp.online) {
            lv_label_set_text(s_ready_status, "Server not reachable");
        } else {
            lv_label_set_text_fmt(s_ready_status, "%.20s  " LV_SYMBOL_OK " server", ns.ssid);
        }
    }

    if (tick % 20 == 1) {  /* 每 2 秒刷新顶栏 */
        int pct = board_battery_percent();
        if (pct >= 0) {
            lv_label_set_text_fmt(s_top_left, "%s %d%%%s", battery_symbol(pct), pct,
                                  board_is_charging() ? " " LV_SYMBOL_CHARGE : "");
        } else {
            lv_label_set_text(s_top_left, LV_SYMBOL_USB);
        }
    }

    uint32_t sec = cap.elapsed_ms / 1000;
    bool sending = !xp.session_done || ring_used() > 0;

    if (ns.state == NET_SETUP) {
        show_view(V_SETUP);
        return;
    }

    if (cap.recording) {
        show_view(V_REC);
        if (http) {
            uint32_t queued = ring_used();
            if (xp.online && queued <= 2) lv_label_set_text(s_rec_net, LV_SYMBOL_UPLOAD " live");
            else lv_label_set_text_fmt(s_rec_net, LV_SYMBOL_WARNING " offline  %lu s buffered", (unsigned long)queued);
            lv_obj_set_style_text_color(s_rec_net, xp.online && queued <= 2 ? COLOR_DIM : COLOR_AMBER, 0);
        } else {
            lv_label_set_text(s_rec_net, "");
        }
        lv_label_set_text_fmt(s_rec_time, "%02lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
        lv_label_set_text_fmt(s_rec_marks, LV_SYMBOL_BELL "  %lu", (unsigned long)cap.marks);
        lv_obj_set_style_text_color(s_rec_marks, s_mark_flash ? COLOR_AMBER : COLOR_TEXT, 0);
        if (s_mark_flash) s_mark_flash--;
        if (tick % 10 < 6) lv_obj_clear_flag(s_rec_dot, LV_OBJ_FLAG_HIDDEN);
        else               lv_obj_add_flag(s_rec_dot, LV_OBJ_FLAG_HIDDEN);
        int lvl = (int)cap.level_db;
        lv_bar_set_value(s_rec_level, lvl < -60 ? -60 : lvl, LV_ANIM_OFF);
    } else if (cap.session == 0 || cap.session == s_dismissed_session) {
        show_view(V_READY);
    } else if (sending) {
        show_view(V_SENDING);
        bool waiting = http && (ns.state != NET_CONNECTED || !xp.online);
        lv_label_set_text(s_send_title, waiting ? LV_SYMBOL_WIFI "  Waiting for Wi-Fi" : LV_SYMBOL_UPLOAD "  Uploading");
        uint32_t total = cap.chunks ? cap.chunks : 1;
        lv_bar_set_range(s_send_bar, 0, (int32_t)total);
        lv_bar_set_value(s_send_bar, (int32_t)xp.sent_chunks, LV_ANIM_OFF);
        lv_label_set_text_fmt(s_send_label, "%lu / %lu s", (unsigned long)xp.sent_chunks, (unsigned long)total);
    } else {
        show_view(V_DONE);
        lv_label_set_text(s_done_title, http ? "Uploaded" : "Done");
        if (cap.dropped) {
            lv_label_set_text_fmt(s_done_info, LV_SYMBOL_WARNING " %02lu:%02lu  ·  %lu s lost",
                                  (unsigned long)(sec / 60), (unsigned long)(sec % 60), (unsigned long)cap.dropped);
        } else {
            lv_label_set_text_fmt(s_done_info, "%02lu:%02lu  ·  %lu marks",
                                  (unsigned long)(sec / 60), (unsigned long)(sec % 60), (unsigned long)cap.marks);
        }
    }
}

/* ---------- 构建 ---------- */

static void build_screen(void)
{
    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, COLOR_BG, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    s_top_left = mk_label(scr, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_top_left, LV_ALIGN_TOP_LEFT, 12, 8);
    s_top_right = mk_label(scr, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_top_right, LV_ALIGN_TOP_RIGHT, -12, 8);

    /* READY */
    lv_obj_t *v = s_view[V_READY] = mk_view(scr);
    lv_obj_t *rec = mk_button(v, 120, 120, COLOR_RED, LV_RADIUS_CIRCLE, "REC", &lv_font_montserrat_20, on_rec_tap);
    lv_obj_align(rec, LV_ALIGN_CENTER, 0, -22);
    s_ready_status = mk_label(v, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_ready_status, LV_ALIGN_BOTTOM_MID, 0, -46);
    lv_obj_t *wifi = mk_button(v, 120, 34, COLOR_PANEL, 17, LV_SYMBOL_WIFI " Wi-Fi setup", &lv_font_montserrat_14, on_wifi_tap);
    lv_obj_align(wifi, LV_ALIGN_BOTTOM_MID, 0, -6);

    /* REC */
    v = s_view[V_REC] = mk_view(scr);
    s_rec_time = mk_label(v, &lv_font_montserrat_48, COLOR_TEXT, "00:00");
    lv_obj_align(s_rec_time, LV_ALIGN_TOP_MID, 10, 4);
    s_rec_dot = lv_obj_create(v);
    lv_obj_set_size(s_rec_dot, 14, 14);
    lv_obj_set_style_radius(s_rec_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_rec_dot, COLOR_RED, 0);
    lv_obj_set_style_border_width(s_rec_dot, 0, 0);
    lv_obj_align_to(s_rec_dot, s_rec_time, LV_ALIGN_OUT_LEFT_MID, -8, 0);
    s_rec_level = lv_bar_create(v);
    lv_obj_set_size(s_rec_level, 180, 8);
    lv_bar_set_range(s_rec_level, -60, 0);
    lv_obj_set_style_bg_color(s_rec_level, COLOR_PANEL, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_rec_level, COLOR_GREEN, LV_PART_INDICATOR);
    lv_obj_align(s_rec_level, LV_ALIGN_TOP_MID, 0, 74);
    s_rec_marks = mk_label(v, &lv_font_montserrat_20, COLOR_TEXT, "");
    lv_obj_align(s_rec_marks, LV_ALIGN_TOP_MID, 0, 88);
    s_rec_net = mk_label(v, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_rec_net, LV_ALIGN_TOP_MID, 0, 114);
    lv_obj_t *mark = mk_button(v, 100, 64, COLOR_AMBER, 14, LV_SYMBOL_BELL " MARK", &lv_font_montserrat_14, on_mark_tap);
    lv_obj_align(mark, LV_ALIGN_BOTTOM_LEFT, 12, -10);
    lv_obj_t *stop = mk_button(v, 100, 64, COLOR_RED, 14, LV_SYMBOL_STOP " STOP", &lv_font_montserrat_14, on_stop_tap);
    lv_obj_align(stop, LV_ALIGN_BOTTOM_RIGHT, -12, -10);

    /* SENDING */
    v = s_view[V_SENDING] = mk_view(scr);
    s_send_title = mk_label(v, &lv_font_montserrat_20, COLOR_AMBER, LV_SYMBOL_UPLOAD "  Uploading");
    lv_obj_align(s_send_title, LV_ALIGN_CENTER, 0, -40);
    s_send_bar = lv_bar_create(v);
    lv_obj_set_size(s_send_bar, 180, 12);
    lv_obj_set_style_bg_color(s_send_bar, COLOR_PANEL, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_send_bar, COLOR_AMBER, LV_PART_INDICATOR);
    lv_obj_align(s_send_bar, LV_ALIGN_CENTER, 0, 0);
    s_send_label = mk_label(v, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_send_label, LV_ALIGN_CENTER, 0, 26);

    /* DONE */
    v = s_view[V_DONE] = mk_view(scr);
    lv_obj_t *ok = lv_obj_create(v);
    lv_obj_set_size(ok, 84, 84);
    lv_obj_set_style_radius(ok, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(ok, COLOR_GREEN, 0);
    lv_obj_set_style_border_width(ok, 0, 0);
    lv_obj_clear_flag(ok, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(ok, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_center(mk_label(ok, &lv_font_montserrat_48, lv_color_white(), LV_SYMBOL_OK));
    s_done_title = mk_label(v, &lv_font_montserrat_20, COLOR_TEXT, "Done");
    lv_obj_align(s_done_title, LV_ALIGN_TOP_MID, 0, 90);
    s_done_info = mk_label(v, &lv_font_montserrat_14, COLOR_DIM, "");
    lv_obj_align(s_done_info, LV_ALIGN_TOP_MID, 0, 118);
    lv_obj_t *nw = mk_button(v, 140, 44, COLOR_PANEL, 22, LV_SYMBOL_PLUS " NEW", &lv_font_montserrat_14, on_new_tap);
    lv_obj_align(nw, LV_ALIGN_BOTTOM_MID, 0, -10);

    /* SETUP */
    v = s_view[V_SETUP] = mk_view(scr);
    s_setup_qr = lv_qrcode_create(v, 112, lv_color_black(), lv_color_white());
    lv_obj_set_style_border_color(s_setup_qr, lv_color_white(), 0);
    lv_obj_set_style_border_width(s_setup_qr, 6, 0);
    lv_obj_align(s_setup_qr, LV_ALIGN_TOP_MID, 0, 0);
    s_setup_ssid = mk_label(v, &lv_font_montserrat_14, COLOR_TEXT, "");
    lv_obj_set_style_text_align(s_setup_ssid, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_setup_ssid, LV_ALIGN_TOP_MID, 0, 132);

    show_view(V_READY);
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
    lv_disp_t *disp = lvgl_port_add_disp(&disp_cfg);

    esp_lcd_touch_handle_t tp = board_touch_init();
    if (tp) {
        lvgl_port_touch_cfg_t touch_cfg = { .disp = disp, .handle = tp };
        lvgl_port_add_touch(&touch_cfg);
    }

    if (lvgl_port_lock(0)) {
        build_screen();
        lvgl_port_unlock();
    }
}

static void setup_fill(void)
{
    net_status_t ns;
    net_get_status(&ns);
    if (ns.state != NET_SETUP) return;
    char qr[64];
    snprintf(qr, sizeof(qr), "WIFI:T:nopass;S:%s;;", ns.ap_ssid);
    lv_qrcode_update(s_setup_qr, qr, strlen(qr));
    lv_label_set_text_fmt(s_setup_ssid, "1. Scan or join  %s\n2. Open  192.168.4.1", ns.ap_ssid);
}

void ui_net_ready(void)
{
    if (lvgl_port_lock(100)) {
        setup_fill();
        lvgl_port_unlock();
    }
}

void ui_request_setup(void)
{
    if (lvgl_port_lock(100)) {
        lv_obj_clean(lv_scr_act());
        lv_obj_t *l = mk_label(lv_scr_act(), &lv_font_montserrat_20, COLOR_TEXT, LV_SYMBOL_WIFI " Wi-Fi setup ...");
        lv_obj_center(l);
        lvgl_port_unlock();
    }
    vTaskDelay(pdMS_TO_TICKS(400));
    net_request_setup();
}

void ui_dismiss_done(void)
{
    capture_status_t cap;
    capture_get_status(&cap);
    s_dismissed_session = cap.session;
}

void ui_show_power_off(void)
{
    if (lvgl_port_lock(100)) {
        lv_obj_clean(lv_scr_act());
        lv_obj_t *l = mk_label(lv_scr_act(), &lv_font_montserrat_20, COLOR_TEXT, LV_SYMBOL_POWER " Power off");
        lv_obj_center(l);
        lvgl_port_unlock();
    }
}
