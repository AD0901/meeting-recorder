idf_component_register(
    SRCS "main.c" "board.c" "chunk_ring.c" "audio_capture.c" "ui.c" "transport.c" "net_config.c" "wifi_mgr.c" "sd_store.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_driver_spi esp_driver_ledc
             esp_lcd esp_adc esp_rom esp_timer esp_hw_support mbedtls
             esp_wifi esp_netif esp_event esp_http_client esp_http_server nvs_flash
             fatfs json)
