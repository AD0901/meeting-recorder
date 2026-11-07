idf_component_register(
    SRCS "main.c" "board.c" "chunk_ring.c" "audio_capture.c" "ui.c" "transport_serial.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_driver_spi esp_driver_ledc
             esp_lcd esp_adc esp_rom esp_timer esp_hw_support mbedtls)
