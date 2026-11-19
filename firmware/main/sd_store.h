/*
 * TF 卡存储（里程碑 5）：录音先写卡，确认 Save 后才上传，云端收齐后删卡上文件。
 *
 *   /sdcard/rec/<id>.pcm   裸 PCM（16kHz / 16bit / 单声道）
 *   /sdcard/rec/<id>.json  元数据：状态、时长、打点、云端 session_id、上传断点 next_seq
 *
 * 状态机：recording → confirm（录完待确认）→ pending（已 Save，待上传）→ 上传完成删除
 *   - Discard：直接删文件，音频不出设备
 *   - 启动扫描：pending 自动补传；confirm / recording（断电）保持待确认，由用户决定，
 *     未经 Save 的录音绝不自动上传
 *
 * 本模块还是环形缓冲的唯一消费者（writer 任务）：采集 → ring → 写卡。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define SD_MAX_MARKS 64
#define SD_UPLOAD_CHUNK_BYTES 320000   /* 上传分片：10 秒（减少公网往返次数，比 1 秒片快约 10 倍） */

typedef enum {
    REC_RECORDING = 0,   /* 正在写卡（断电后重启也按待确认处理） */
    REC_CONFIRM,         /* 录完，等待 Save / Discard */
    REC_PENDING,         /* 已 Save，等待 / 正在上传 */
} rec_status_t;

typedef struct {
    uint32_t    id;
    rec_status_t status;
    uint32_t    duration_ms;
    uint32_t    total_chunks;              /* 按 chunk_bytes 算出的上传片数 */
    uint32_t    chunk_bytes;               /* 上传分片字节数（新录音 320000=10 秒；旧 meta 缺省 32000） */
    uint32_t    marks[SD_MAX_MARKS];       /* offset_ms */
    uint32_t    mark_count;
    char        session_id[48];            /* 云端会话 id，空 = 还没创建 */
    uint32_t    next_seq;                  /* 下一片要传的 seq（之前的已被云端确认） */
} rec_meta_t;

typedef struct {
    bool     card_ok;
    bool     write_error;        /* 写卡出错（卡满 / 卡被拔出等） */
    bool     await_confirm;      /* 有一段录音等待 Save / Discard */
    uint32_t confirm_duration_s;
    uint32_t confirm_marks;
    uint32_t pending;            /* 待上传段数（含正在传的） */
    bool     saved_active;       /* 最近 Save 的那段还在上传队列里 */
    uint32_t saved_total;        /* 最近 Save 段的总片数 */
    uint32_t saved_duration_s;
    uint32_t saved_marks;
} sd_status_t;

bool sd_store_init(void);        /* 挂卡 + 恢复扫描 + 启动 writer 任务；返回卡是否可用 */
void sd_store_get_status(sd_status_t *out);

/* 录完确认（UI 按钮和侧键共用） */
void sd_store_save(void);        /* confirm → pending，唤醒上传 */
void sd_store_discard(void);     /* 删除 confirm 段的文件 */

/* 上传任务接口（单消费者，按 id 从小到大逐段处理） */
bool sd_store_claim_pending(rec_meta_t *out);   /* 取最早一段 pending 并占用；false = 没有 */
void sd_store_set_session(const char *session_id);  /* 记下云端 session_id（断点续传用） */
void sd_store_progress(uint32_t next_seq);      /* 更新断点，每 8 片落一次 meta */
void sd_store_release(void);                    /* 中途失败：保存断点，释放占用 */
void sd_store_finish(void);                     /* 上传完成（或被云端拒绝）：删卡上文件 */
