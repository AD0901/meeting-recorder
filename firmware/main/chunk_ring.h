/*
 * PSRAM 环形缓冲：采集任务（唯一生产者）→ 传输任务（唯一消费者）
 *
 * 每个槽位是一条有序消息：会话开始 / 1 秒音频 / 打点 / 会话结束。
 * 控制消息和音频走同一条队列，保证顺序一致；里程碑 3 换成 HTTP 上传时队列不用改。
 * 消费者先 peek 处理，成功后再 release；发送失败时数据仍留在队列里，可以重试。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"

#define CHUNK_SAMPLES     16000                        /* 1 秒 */
#define CHUNK_MAX_BYTES   (CHUNK_SAMPLES * 2)          /* 16kHz 单声道 s16le */

typedef enum {
    CHUNK_BEGIN = 0,   /* seq 未用 */
    CHUNK_AUDIO,       /* seq = 分片序号，从 0 递增 */
    CHUNK_MARK,        /* seq = offset_ms */
    CHUNK_END,         /* seq = last_seq（无音频时为 UINT32_MAX） */
} chunk_type_t;

typedef struct {
    chunk_type_t type;
    uint32_t session;      /* 设备本地会话号 */
    uint32_t seq;
    uint32_t len;          /* data 中有效字节数 */
    int64_t  t_us;         /* 入队时刻（esp_timer） */
    uint8_t  data[CHUNK_MAX_BYTES];
} chunk_t;

bool     ring_init(size_t slots);

/* 生产者 */
chunk_t *ring_acquire(void);           /* 满了返回 NULL */
void     ring_commit(void);

/* 消费者 */
chunk_t *ring_peek(TickType_t wait);   /* 空返回 NULL */
void     ring_release(void);

size_t   ring_used(void);
size_t   ring_capacity(void);
