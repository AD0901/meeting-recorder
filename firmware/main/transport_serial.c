#include <stdbool.h>
#include <stdio.h>

#include "transport_serial.h"

#include "chunk_ring.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"

static const char *TAG = "xport";

static char s_device_id[13];
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static transport_status_t s_st;
static unsigned char *s_b64;
#define B64_CAP (CHUNK_MAX_BYTES * 4 / 3 + 8)

const char *transport_device_id(void) { return s_device_id; }

void transport_get_status(transport_status_t *out)
{
    portENTER_CRITICAL(&s_lock);
    *out = s_st;
    portEXIT_CRITICAL(&s_lock);
}

static void send_chunk(const chunk_t *c)
{
    switch (c->type) {
    case CHUNK_BEGIN:
        printf("\n@@BEGIN session=%lu device_id=%s sample_rate=16000 channels=1 format=pcm_s16le\n",
               (unsigned long)c->session, s_device_id);
        portENTER_CRITICAL(&s_lock);
        s_st = (transport_status_t){ .session = c->session };
        portEXIT_CRITICAL(&s_lock);
        break;

    case CHUNK_AUDIO: {
        int64_t t0 = esp_timer_get_time();
        size_t olen = 0;
        mbedtls_base64_encode(s_b64, B64_CAP, &olen, c->data, c->len);
        uint32_t crc = esp_rom_crc32_le(0, c->data, c->len);
        flockfile(stdout);  /* 整行不能被其他任务的日志插进来 */
        printf("@@CHUNK session=%lu seq=%lu bytes=%lu crc32=%08lx b64=",
               (unsigned long)c->session, (unsigned long)c->seq,
               (unsigned long)c->len, (unsigned long)crc);
        fwrite(s_b64, 1, olen, stdout);
        fputc('\n', stdout);
        fflush(stdout);
        funlockfile(stdout);
        uint32_t ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        portENTER_CRITICAL(&s_lock);
        s_st.sent_chunks++;
        s_st.last_seq_sent = c->seq;
        s_st.last_send_ms = ms;
        portEXIT_CRITICAL(&s_lock);
        uint32_t lag_ms = (uint32_t)((esp_timer_get_time() - c->t_us) / 1000);
        ESP_LOGI(TAG, "seq %lu sent %lu B in %lu ms, queued %lu ms, ring %u/%u",
                 (unsigned long)c->seq, (unsigned long)c->len, (unsigned long)ms,
                 (unsigned long)lag_ms, (unsigned)ring_used() - 1, (unsigned)ring_capacity());
        break;
    }

    case CHUNK_MARK:
        printf("@@MARK session=%lu offset_ms=%lu\n", (unsigned long)c->session, (unsigned long)c->seq);
        break;

    case CHUNK_END:
        printf("@@END session=%lu last_seq=%ld\n", (unsigned long)c->session, (long)(int32_t)c->seq);
        fflush(stdout);
        portENTER_CRITICAL(&s_lock);
        s_st.session_done = true;
        portEXIT_CRITICAL(&s_lock);
        break;
    }
}

static void transport_task(void *arg)
{
    while (1) {
        chunk_t *c = ring_peek(pdMS_TO_TICKS(500));
        if (!c) continue;
        send_chunk(c);   /* 串口版不会失败；HTTP 版失败时不 release，下次重试 */
        ring_release();
    }
}

void transport_start_task(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_device_id, sizeof(s_device_id), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    s_b64 = heap_caps_malloc(B64_CAP, MALLOC_CAP_SPIRAM);
    assert(s_b64);
    s_st.session_done = true;
    xTaskCreatePinnedToCore(transport_task, "xport", 6144, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "serial transport ready, device_id=%s", s_device_id);
}
