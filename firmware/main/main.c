/*
 * 会议录音器固件 — 里程碑 5：TF 卡离线缓冲 + 录完确认保存
 *
 *   屏幕：点 REC 开始；录音中点 MARK 打点、点 STOP 停止；
 *         停止后进入确认页：SAVE 才上传（断网则存卡等有网自动补传），DEL 直接删除不上传；
 *         上传中可点 Later 回待机后台传；待机页右下角 Wi-Fi 按钮：进入配网模式
 *   PLUS（右键）短按：开始 / 停止录音；确认页 = Save
 *   BOOT（左键）短按：录音中打点；确认页 = Discard；待机时长按 3 秒：进入配网模式
 *   PWR （中键）：电池供电时按一下开机（硬件上电 + GPIO2 自锁）；长按 3 秒关机
 *
 * 数据流：ES7210 →[capture, CPU1]→ PSRAM 环形缓冲 →[sd_writer, CPU0]→ TF 卡
 *         TF 卡（已 Save 的段）→[transport, CPU0]→ Wi-Fi HTTP / USB 串口 → 云端收齐后删卡上文件
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
#include "sd_store.h"
#include "transport.h"
#include "ui.h"
#include "wifi_mgr.h"

static const char *TAG = "main";

#define RING_SLOTS 120   /* 120 x 1s ≈ 3.7MB PSRAM：采集 → 写卡的抖动缓冲（吸收 SD 写入延迟） */

static button_handle_t s_btn_plus, s_btn_boot, s_btn_pwr;

static void on_plus(void *btn, void *arg)
{
    net_status_t ns;
    net_get_status(&ns);
    if (ns.state == NET_SETUP) return;   /* 配网模式下不录音 */
    sd_status_t sd;
    sd_store_get_status(&sd);
    if (sd.await_confirm) {              /* 确认页：PLUS = Save */
        ui_confirm_save();
        return;
    }
    capture_status_t st;
    capture_get_status(&st);
    if (st.recording) {
        capture_request_stop();
    } else if (sd.card_ok) {             /* 无卡：READY 状态行已提示插卡 */
        capture_request_start();
    }
}

static void on_boot(void *btn, void *arg)
{
    sd_status_t sd;
    sd_store_get_status(&sd);
    if (sd.await_confirm) {              /* 确认页：BOOT = Discard */
        ui_confirm_discard();
        return;
    }
    capture_status_t st;
    capture_get_status(&st);
    if (st.recording) capture_request_mark();
}

static void on_boot_long(void *btn, void *arg)
{
    capture_status_t st;
    capture_get_status(&st);
    sd_status_t sd;
    sd_store_get_status(&sd);
    if (!st.recording && !sd.await_confirm) ui_request_setup();
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
    static button_event_args_t pwr_long = { .long_press.press_time = 3000 };  /* 长按 3 秒才关机 */
    iot_button_register_cb(s_btn_pwr, BUTTON_LONG_PRESS_START, &pwr_long, on_pwr_long, NULL);
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
    board_init();  /* 最先执行：尽快拉高 GPIO2 电源保持，PWR 开机后松手不掉电 */

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    bool setup = net_setup_requested();

    if (!ring_init(RING_SLOTS)) abort();
    sd_store_init();   /* 挂 TF 卡 + 恢复扫描（pending 补传 / 断电段待确认）；无卡则禁止录音 */

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
