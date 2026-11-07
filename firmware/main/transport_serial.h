/*
 * 传输（里程碑 2 版本）：从环形缓冲取消息，按行写到 USB 串口，
 * 由电脑端 tools/serial_capture.py 按 seq 拼成 WAV。
 * 消息格式与第 5 节上传协议一一对应，里程碑 3 会换成 HTTP 版本。
 *
 *   @@BEGIN session=1 device_id=7c0c5f48f9ac sample_rate=16000 channels=1 format=pcm_s16le
 *   @@CHUNK session=1 seq=0 bytes=32000 crc32=xxxxxxxx b64=....
 *   @@MARK  session=1 offset_ms=12345
 *   @@END   session=1 last_seq=29
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t session;
    uint32_t sent_chunks;   /* 本会话已发出的音频分片 */
    uint32_t last_seq_sent;
    uint32_t last_send_ms;  /* 最近一片发送耗时 */
    bool     session_done;  /* 已发出 END */
} transport_status_t;

void transport_start_task(void);
void transport_get_status(transport_status_t *out);
const char *transport_device_id(void);
