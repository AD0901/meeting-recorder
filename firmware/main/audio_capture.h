/*
 * 采集任务（固定在 CPU1）：持续读 ES7210，录音时把 MIC1/MIC2 平均成单声道，
 * 每满 1 秒写入一个环形缓冲槽位。开始 / 停止 / 打点也由本任务按采样时间顺序写入队列。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool     recording;
    bool     paused;        /* 暂停中：继续读麦克风但不写卡，计时冻结，恢复后音频无缝拼接 */
    uint32_t session;       /* 当前（或最近一次）会话号 */
    uint32_t elapsed_ms;    /* 本次已录时长（不含暂停时间） */
    uint32_t marks;         /* 本次打点数 */
    uint32_t chunks;        /* 本次已入队的音频分片数 */
    uint32_t dropped;       /* 缓冲满被丢弃的分片数（应为 0） */
    float    level_db;      /* 最近 20ms 电平，dBFS */
} capture_status_t;

void capture_start_task(void);
void capture_request_start(void);
void capture_request_stop(void);
void capture_request_mark(void);
void capture_request_pause(void);   /* 录音中暂停 / 继续（幂等翻转） */
void capture_get_status(capture_status_t *out);
