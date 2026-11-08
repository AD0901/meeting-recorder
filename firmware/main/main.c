/*
 * 会议录音器固件 — 里程碑 3：Wi-Fi 分片上传
 *
 *   屏幕：点 REC 开始；录音中点 MARK 打点、点 STOP 停止；结束后显示 Done，点 NEW 回到待机
 *         待机页右下角 Wi-Fi 按钮：进入配网模式
 *   PLUS（右键）短按：开始 / 停止录音
 *   BOOT（左键）短按：录音中打点；待机时长按 3 秒：进入配网模式
 *   PWR （中键）长按：关机（保持官方行为，仅电池供电时生效）
 *
 * 数据流：ES7210 →[capture, CPU1]→ PSRAM 环形缓冲 →[transport, CPU0]→ Wi-Fi HTTP 上传
 *         （没配网络时退回 USB 串口，和里程碑 2 一样）
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
#include "nvs_flash.h"
#include "transport.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "main";

#define RING_SLOTS 120   /* 120 x 1s ≈ 3.7MB PSRAM，可扛约 2 分钟的传输中断 */

static button_handle_t s_btn_plus, s_btn_boot, s_btn_pwr;

static void on_plus(void *btn, void *arg)
{
    net_status_t ns;
    net_get_status(&ns);
    if (ns.state == NET_SETUP) return;   /* 配网模式下不录音 */
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

static void on_boot_long(void *btn, void *arg)
{
    capture_status_t st;
    capture_get_status(&st);
    if (!st.recording) ui_request_setup();
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
    button_event_args_t la = { .long_press.press_time = 3000 };
    iot_button_register_cb(s_btn_boot, BUTTON_LONG_PRESS_START, &la, on_boot_long, NULL);
    iot_button_register_cb(s_btn_pwr,  BUTTON_PRESS_UP,     NULL, on_pwr_first_release, NULL);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    bool setup = net_setup_requested();

    board_init();
    if (!ring_init(RING_SLOTS)) abort();

    esp_lcd_panel_io_handle_t io;
    esp_lcd_panel_handle_t panel;
    board_lcd_init(&io, &panel, BOARD_LCD_W * 40 * 2);
    ui_init(io, panel);
    board_backlight(80);

    net_start(setup);
    ui_net_ready();
    transport_start_task();
    capture_start_task();
    buttons_init();

    ESP_LOGI(TAG, "ready. free internal %u KB, PSRAM %u KB",
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
}
