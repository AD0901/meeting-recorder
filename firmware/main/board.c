#include "board.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/i2s_tdm.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_codec_dev_defaults.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_touch_cst816s.h"
#include "esp_log.h"

static const char *TAG = "board";

/* ---- 引脚 ---- */
#define PIN_PWR_HOLD    2
#define PIN_BAT_ADC     1   /* ADC1_CH0 */
#define PIN_CHARGING    3   /* 低 = 充电中 */
#define PIN_I2C_SDA     42
#define PIN_I2C_SCL     41
#define PIN_I2S_MCLK    8
#define PIN_I2S_BCLK    9
#define PIN_I2S_WS      10
#define PIN_I2S_DOUT    12
#define PIN_I2S_DIN     11
#define PIN_PA_EN       7
#define PIN_LCD_MOSI    39
#define PIN_LCD_SCLK    38
#define PIN_LCD_CS      21
#define PIN_LCD_DC      45
#define PIN_LCD_RST     40
#define PIN_LCD_BL      46
#define PIN_TP_INT      48
#define PIN_TP_RST      47

static i2c_master_bus_handle_t s_i2c;
static esp_codec_dev_handle_t s_mic;
static esp_codec_dev_handle_t s_spk;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_adc_cali;

void board_power_hold(bool on)
{
    gpio_set_level(PIN_PWR_HOLD, on ? 1 : 0);
}

static void power_init(void)
{
    gpio_config_t out = { .pin_bit_mask = 1ULL << PIN_PWR_HOLD, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&out);
    board_power_hold(true);  /* 电池供电时必须尽快拉高 */

    gpio_config_t in = {
        .pin_bit_mask = 1ULL << PIN_CHARGING,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&in);

    adc_oneshot_unit_init_cfg_t unit = { .unit_id = ADC_UNIT_1 };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit, &s_adc));
    adc_oneshot_chan_cfg_t ch = { .bitwidth = ADC_BITWIDTH_DEFAULT, .atten = ADC_ATTEN_DB_12 };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, ADC_CHANNEL_0, &ch));
    adc_cali_curve_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1, .chan = ADC_CHANNEL_0,
        .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali, &s_adc_cali) != ESP_OK) {
        s_adc_cali = NULL;
        ESP_LOGW(TAG, "battery ADC calibration unavailable");
    }
}

static void audio_init(i2c_master_bus_handle_t bus)
{
    /* I2S 全双工：TX = STD 立体声 → ES8311，RX = TDM 4 slot ← ES7210 */
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 16;  /* 16 x 20ms = 320ms 余量，防止其他任务短暂占用 CPU 时丢音 */
    chan_cfg.dma_frame_num = 320;   /* 20ms @16kHz */
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx, &rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = BOARD_SAMPLE_RATE,
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
            .sample_rate_hz = BOARD_SAMPLE_RATE,
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

    audio_codec_i2s_cfg_t i2s_if_cfg = { .port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    audio_codec_i2c_cfg_t i2c_cfg = { .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = bus };
    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg),
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = PIN_PA_EN,
        .use_mclk = true,
        .hw_gain = { .pa_voltage = 5.0, .codec_dac_voltage = 3.3 },
    };
    esp_codec_dev_cfg_t spk_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = data_if,
    };
    s_spk = esp_codec_dev_new(&spk_cfg);

    i2c_cfg.addr = ES7210_CODEC_DEFAULT_ADDR;
    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = audio_codec_new_i2c_ctrl(&i2c_cfg),
        .mic_selected = ES7120_SEL_MIC1 | ES7120_SEL_MIC2 | ES7120_SEL_MIC3 | ES7120_SEL_MIC4,
    };
    esp_codec_dev_cfg_t mic_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = es7210_codec_new(&es7210_cfg),
        .data_if = data_if,
    };
    s_mic = esp_codec_dev_new(&mic_cfg);
    assert(s_spk && s_mic);

    esp_codec_dev_sample_info_t out_fs = { .bits_per_sample = 16, .channel = 1, .sample_rate = BOARD_SAMPLE_RATE };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_spk, &out_fs));
    ESP_ERROR_CHECK(esp_codec_dev_set_out_vol(s_spk, 85));

    esp_codec_dev_sample_info_t in_fs = {
        .bits_per_sample = 16, .channel = BOARD_IN_CH, .channel_mask = 0, .sample_rate = BOARD_SAMPLE_RATE,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_mic, &in_fs));
    ESP_ERROR_CHECK(esp_codec_dev_set_in_gain(s_mic, BOARD_IN_GAIN_DB));
}

void board_init(void)
{
    power_init();

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &s_i2c));

    audio_init(s_i2c);
    ESP_LOGI(TAG, "board ready (mic gain %.1f dB, MIC1=slot%d MIC2=slot%d)",
             BOARD_IN_GAIN_DB, BOARD_MIC1_SLOT, BOARD_MIC2_SLOT);
}

esp_codec_dev_handle_t board_mic(void) { return s_mic; }
esp_codec_dev_handle_t board_speaker(void) { return s_spk; }

void board_lcd_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel, size_t max_transfer_bytes)
{
    spi_bus_config_t bus = {
        .sclk_io_num = PIN_LCD_SCLK,
        .mosi_io_num = PIN_LCD_MOSI,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = max_transfer_bytes,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO));

    esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = PIN_LCD_CS,
        .dc_gpio_num = PIN_LCD_DC,
        .spi_mode = 3,
        .pclk_hz = 40 * 1000 * 1000,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io_cfg, io));

    esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = PIN_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(*io, &panel_cfg, panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(*panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(*panel));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(*panel, true));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(*panel, true));

    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .timer_num = LEDC_TIMER_0,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));
    ledc_channel_config_t ch = {
        .gpio_num = PIN_LCD_BL,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .timer_sel = LEDC_TIMER_0,
        .duty = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&ch));
}

void board_backlight(uint8_t percent)
{
    if (percent > 100) percent = 100;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, (1023 * percent) / 100);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

int board_battery_mv(void)
{
    int raw = 0, mv = 0;
    if (!s_adc || adc_oneshot_read(s_adc, ADC_CHANNEL_0, &raw) != ESP_OK) return -1;
    if (s_adc_cali && adc_cali_raw_to_voltage(s_adc_cali, raw, &mv) == ESP_OK) {
        return mv * 3;  /* 板上 1/3 分压（同官方示例） */
    }
    return -1;
}

int board_battery_percent(void)
{
    /* 与官方示例相同的分段表：3.52/3.64/3.76/3.88/4.0V */
    int mv = board_battery_mv();
    if (mv < 0) return -1;
    if (mv < 3520) return 1;
    if (mv < 3640) return 20;
    if (mv < 3760) return 40;
    if (mv < 3880) return 60;
    if (mv < 4000) return 80;
    return 100;
}

bool board_is_charging(void)
{
    return gpio_get_level(PIN_CHARGING) == 0;
}

esp_lcd_touch_handle_t board_touch_init(void)
{
    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_i2c_config_t io_cfg = ESP_LCD_TOUCH_IO_I2C_CST816S_CONFIG();
    io_cfg.scl_speed_hz = 400 * 1000;
    if (esp_lcd_new_panel_io_i2c(s_i2c, &io_cfg, &io) != ESP_OK) return NULL;
    esp_lcd_touch_config_t cfg = {
        .x_max = BOARD_LCD_W,
        .y_max = BOARD_LCD_H,
        .rst_gpio_num = PIN_TP_RST,
        .int_gpio_num = PIN_TP_INT,
    };
    esp_lcd_touch_handle_t tp = NULL;
    if (esp_lcd_touch_new_i2c_cst816s(io, &cfg, &tp) != ESP_OK) {
        ESP_LOGW(TAG, "touch init failed, buttons only");
        return NULL;
    }
    return tp;
}
