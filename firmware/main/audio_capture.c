#include "audio_capture.h"

#include <math.h>
#include <string.h>

#include "board.h"
#include "chunk_ring.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "capture";

#define FRAME_SAMPLES  320                                /* 20ms */
#define FRAME_BYTES    (FRAME_SAMPLES * BOARD_IN_CH * 2)

enum { REQ_START = 1 << 0, REQ_STOP = 1 << 1, REQ_MARK = 1 << 2 };
static volatile uint32_t s_req;

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static capture_status_t s_st;

void capture_request_start(void) { __atomic_or_fetch(&s_req, REQ_START, __ATOMIC_SEQ_CST); }
void capture_request_stop(void)  { __atomic_or_fetch(&s_req, REQ_STOP,  __ATOMIC_SEQ_CST); }
void capture_request_mark(void)  { __atomic_or_fetch(&s_req, REQ_MARK,  __ATOMIC_SEQ_CST); }

void capture_get_status(capture_status_t *out)
{
    portENTER_CRITICAL(&s_lock);
    *out = s_st;
    portEXIT_CRITICAL(&s_lock);
}

/* 推一条控制消息（开始 / 打点 / 结束）。满了就重试，控制消息不能丢。 */
static void push_ctrl(chunk_type_t type, uint32_t session, uint32_t seq)
{
    chunk_t *c;
    while ((c = ring_acquire()) == NULL) {
        ESP_LOGW(TAG, "ring full, waiting to push ctrl %d", type);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    c->type = type;
    c->session = session;
    c->seq = seq;
    c->len = 0;
    ring_commit();
}

/* 把暂存区的一段音频写入环形缓冲。缓冲满时丢弃，但 seq 照样递增，服务端能发现缺口。 */
static void push_audio(const int16_t *pcm, uint32_t n, uint32_t session, uint32_t seq)
{
    chunk_t *c = ring_acquire();
    if (!c) {
        portENTER_CRITICAL(&s_lock);
        s_st.dropped++;
        portEXIT_CRITICAL(&s_lock);
        ESP_LOGE(TAG, "ring FULL, dropping seq %lu", (unsigned long)seq);
        return;
    }
    memcpy(c->data, pcm, n * sizeof(int16_t));
    c->type = CHUNK_AUDIO;
    c->session = session;
    c->seq = seq;
    c->len = n * sizeof(int16_t);
    ring_commit();
}

static void capture_task(void *arg)
{
    static int16_t frame[FRAME_SAMPLES * BOARD_IN_CH];
    esp_codec_dev_handle_t mic = board_mic();

    bool recording = false;
    uint32_t session = 0;
    uint32_t seq = 0;            /* 下一个音频分片序号 */
    uint64_t samples = 0;        /* 本次已录采样数（含被丢弃的） */
    /* 先在暂存区攒满 1 秒，再一次性写入环形缓冲。
     * 不能提前占住环形缓冲的槽位：中途插入的打点 / 结束消息会拿到同一个槽位，导致音频丢失。 */
    int16_t *stage = heap_caps_malloc(CHUNK_MAX_BYTES, MALLOC_CAP_SPIRAM);
    assert(stage);
    uint32_t cur_fill = 0;       /* 暂存区已有采样数 */

    ESP_LOGI(TAG, "capture task on core %d", xPortGetCoreID());

    while (1) {
        if (esp_codec_dev_read(mic, frame, FRAME_BYTES) != ESP_CODEC_DEV_OK) {
            ESP_LOGE(TAG, "mic read error");
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        /* 混成单声道 + 电平 */
        int16_t mono[FRAME_SAMPLES];
        double sq = 0;
        for (int i = 0; i < FRAME_SAMPLES; i++) {
            int32_t v = ((int32_t)frame[i * BOARD_IN_CH + BOARD_MIC1_SLOT] +
                         (int32_t)frame[i * BOARD_IN_CH + BOARD_MIC2_SLOT]) / 2;
            mono[i] = (int16_t)v;
            sq += (double)v * v;
        }
        double rms = sqrt(sq / FRAME_SAMPLES);
        float level = rms < 1 ? -90.f : (float)(20.0 * log10(rms / 32768.0));

        uint32_t req = __atomic_exchange_n(&s_req, 0, __ATOMIC_SEQ_CST);

        if ((req & REQ_START) && !recording) {
            recording = true;
            session++;
            seq = 0;
            samples = 0;
            cur_fill = 0;
            push_ctrl(CHUNK_BEGIN, session, 0);
            portENTER_CRITICAL(&s_lock);
            s_st = (capture_status_t){ .recording = true, .session = session };
            portEXIT_CRITICAL(&s_lock);
            ESP_LOGI(TAG, "session %lu START", (unsigned long)session);
        }

        if (recording && (req & REQ_MARK)) {
            uint32_t offset_ms = (uint32_t)(samples * 1000 / BOARD_SAMPLE_RATE);
            push_ctrl(CHUNK_MARK, session, offset_ms);
            portENTER_CRITICAL(&s_lock);
            s_st.marks++;
            portEXIT_CRITICAL(&s_lock);
        }

        if (recording) {
            for (int i = 0; i < FRAME_SAMPLES; i++) {
                stage[cur_fill++] = mono[i];
                samples++;
                if (cur_fill == CHUNK_SAMPLES) {
                    push_audio(stage, cur_fill, session, seq);
                    seq++;
                    cur_fill = 0;
                }
            }
        }

        if (recording && (req & REQ_STOP)) {
            /* 把不满 1 秒的尾巴也发出去 */
            if (cur_fill > 0) {
                push_audio(stage, cur_fill, session, seq);
                seq++;
            }
            uint32_t last_seq = seq ? seq - 1 : UINT32_MAX;
            push_ctrl(CHUNK_END, session, last_seq);
            recording = false;
            cur_fill = 0;
            ESP_LOGI(TAG, "session %lu STOP: %.1f s, %lu chunks, last_seq %ld",
                     (unsigned long)session, samples / (double)BOARD_SAMPLE_RATE,
                     (unsigned long)seq, (long)(int32_t)last_seq);
        }

        portENTER_CRITICAL(&s_lock);
        s_st.recording = recording;
        s_st.level_db = level;
        if (recording) s_st.elapsed_ms = (uint32_t)(samples * 1000 / BOARD_SAMPLE_RATE);
        s_st.chunks = seq;
        portEXIT_CRITICAL(&s_lock);
    }
}

void capture_start_task(void)
{
    /* CPU1、高优先级：不能被 UI / 传输卡住，否则 I2S DMA 溢出丢音 */
    xTaskCreatePinnedToCore(capture_task, "capture", 8192, NULL, configMAX_PRIORITIES - 3, NULL, 1);
}
