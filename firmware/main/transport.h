/*
 * 传输任务（CPU0）：从 PSRAM 环形缓冲按顺序取消息发出去。
 *
 *   HTTP 模式（配了 Wi-Fi 和服务器时）：按 PROJECT_BRIEF 第 5 节协议上传。
 *     每条消息收到服务端 2xx 才从缓冲里删掉；断网 / 服务器不通时停在原地重试，
 *     录音继续往缓冲里写（120 秒容量），恢复后按顺序补传，所以断网 30 秒不丢音频。
 *   串口模式（没配网络时）：和里程碑 2 一样按行写到 USB 串口，由 tools/serial_capture.py 接收。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { XPORT_SERIAL = 0, XPORT_HTTP } xport_mode_t;

typedef struct {
    xport_mode_t mode;
    uint32_t session;       /* 设备本地会话号 */
    uint32_t sent_chunks;   /* 本会话已被确认的音频分片 */
    uint32_t last_seq_sent;
    uint32_t last_send_ms;  /* 最近一片发送耗时 */
    bool     session_done;  /* END 已被服务端确认（串口模式：已发出） */
    bool     online;        /* 最近一次请求服务端成功 */
    bool     auth_error;    /* 服务端返回 401：口令不对 */
    uint32_t retries;       /* 本会话累计重试次数 */
    char     server_session[48];
} transport_status_t;

void transport_start_task(void);
void transport_get_status(transport_status_t *out);
const char *transport_device_id(void);
