/*
 * 会议录音器固件 — 里程碑 2：按键录音 + 屏幕状态 + 串口导出
 *
 *   屏幕：点 REC 开始；录音中点 MARK 打点、点 STOP 停止；结束后显示 Done，点 NEW 回到待机
 *   PLUS（右键）短按：开始 / 停止录音
 *   BOOT（左键）短按：录音中打点
 *   PWR （中键）长按：关机（保持官方行为，仅电池供电时生效）
 *
 * 数据流：ES7210 →[capture, CPU1]→ PSRAM 环形缓冲 →[transport, CPU0]→ USB 串口
 */
#include "audio_capture.h"
#include "board.h"
#include "button_gpio.h"
#include "chunk_ring.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "iot_button.h"
#include "transport_serial.h"
#include "ui.h"

static const char *TAG = "main";

#define RING_SLOTS 120   /* 120 x 1s ≈ 3.7MB PSRAM，可扛约 2 分钟的传输中断 */

static button_handle_t s_btn_plus, s_btn_boot, s_btn_pwr;

static void on_plus(void *btn, void *arg)
{
    capture_status_t st;
    capture_get_status(&st);
    if (st.recording) {
        capture_request_stop();
    } else {
        ui_dismiss_done();
        capture_request_start();
    }
}

static void on_boot(void *btn, void *arg)
{
    capture_status_t st;
    capture_get_status(&st);
    if (st.recording) capture_request_mark();
}

static void on_pwr_long(void *btn, void *arg)
{
    ESP_LOGW(TAG, "PWR long press -> power off");
    ui_show_power_off();
    vTaskDelay(pdMS_TO_TICKS(300));
    board_power_hold(false);
}

/* 开机时 PWR 键往往还按着，等第一次松开后再挂长按关机，避免一开机就关 */
static void on_pwr_first_release(void *btn, void *arg)
{
    iot_button_register_cb(s_btn_pwr, BUTTON_LONG_PRESS_START, NULL, on_pwr_long, NULL);
    iot_button_unregister_cb(s_btn_pwr, BUTTON_PRESS_UP, NULL);
}

static button_handle_t make_button(int gpio)
{
    button_config_t cfg = {0};
    button_gpio_config_t gcfg = { .gpio_num = gpio, .active_level = 0 };
    button_handle_t h = NULL;
    ESP_ERROR_CHECK(iot_button_new_gpio_device(&cfg, &gcfg, &h));
    return h;
}

static void buttons_init(void)
{
    s_btn_plus = make_button(BOARD_PIN_KEY_PLUS);
    s_btn_boot = make_button(BOARD_PIN_KEY_BOOT);
    s_btn_pwr  = make_button(BOARD_PIN_KEY_PWR);
    iot_button_register_cb(s_btn_plus, BUTTON_SINGLE_CLICK, NULL, on_plus, NULL);
    iot_button_register_cb(s_btn_boot, BUTTON_SINGLE_CLICK, NULL, on_boot, NULL);
    iot_button_register_cb(s_btn_pwr,  BUTTON_PRESS_UP,     NULL, on_pwr_first_release, NULL);
}

void app_main(void)
{
    board_init();
    if (!ring_init(RING_SLOTS)) abort();

    esp_lcd_panel_io_handle_t io;
    esp_lcd_panel_handle_t panel;
    board_lcd_init(&io, &panel, BOARD_LCD_W * 40 * 2);
    ui_init(io, panel);
    board_backlight(80);

    transport_start_task();
    capture_start_task();
    buttons_init();

    ESP_LOGI(TAG, "ready. free internal %u KB, PSRAM %u KB",
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
}
