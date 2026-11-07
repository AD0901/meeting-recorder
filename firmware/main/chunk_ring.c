#include "chunk_ring.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/semphr.h"

static const char *TAG = "ring";

static chunk_t *s_slots;
static size_t s_cap;
static volatile uint32_t s_head;  /* 生产者写入计数 */
static volatile uint32_t s_tail;  /* 消费者读出计数 */
static SemaphoreHandle_t s_avail;

bool ring_init(size_t slots)
{
    s_slots = heap_caps_calloc(slots, sizeof(chunk_t), MALLOC_CAP_SPIRAM);
    if (!s_slots) {
        ESP_LOGE(TAG, "PSRAM alloc failed (%u slots)", (unsigned)slots);
        return false;
    }
    s_cap = slots;
    s_head = s_tail = 0;
    s_avail = xSemaphoreCreateCounting(slots, 0);
    ESP_LOGI(TAG, "%u slots x %u B = %.2f MB in PSRAM (%.0f s of audio)",
             (unsigned)slots, (unsigned)sizeof(chunk_t),
             slots * sizeof(chunk_t) / 1048576.0, (double)slots * CHUNK_SAMPLES / 16000.0);
    return true;
}

chunk_t *ring_acquire(void)
{
    uint32_t head = s_head;
    if (head - __atomic_load_n(&s_tail, __ATOMIC_ACQUIRE) >= s_cap) return NULL;
    chunk_t *c = &s_slots[head % s_cap];
    c->len = 0;
    return c;
}

void ring_commit(void)
{
    s_slots[s_head % s_cap].t_us = esp_timer_get_time();
    __atomic_store_n(&s_head, s_head + 1, __ATOMIC_RELEASE);
    xSemaphoreGive(s_avail);
}

chunk_t *ring_peek(TickType_t wait)
{
    if (__atomic_load_n(&s_head, __ATOMIC_ACQUIRE) == s_tail) {
        /* 等待新数据；信号量计数可能与 head 不完全同步，以 head/tail 为准 */
        xSemaphoreTake(s_avail, wait);
        if (__atomic_load_n(&s_head, __ATOMIC_ACQUIRE) == s_tail) return NULL;
    }
    return &s_slots[s_tail % s_cap];
}

void ring_release(void)
{
    xSemaphoreTake(s_avail, 0);  /* 消费掉对应计数（可能已被 peek 取走） */
    __atomic_store_n(&s_tail, s_tail + 1, __ATOMIC_RELEASE);
}

size_t ring_used(void)
{
    return __atomic_load_n(&s_head, __ATOMIC_ACQUIRE) - __atomic_load_n(&s_tail, __ATOMIC_ACQUIRE);
}

size_t ring_capacity(void) { return s_cap; }
