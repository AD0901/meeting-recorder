/*
 * 传输任务（CPU0，里程碑 5 版）：不再直接消费环形缓冲，而是扫描 TF 卡上
 * 已确认 Save 的录音（status=pending），读文件分片上传，云端收齐后删卡上文件。
 *
 *   HTTP 模式：按 PROJECT_BRIEF 第 5 节协议。断点续传（meta 里记 session_id 和 next_seq），
 *     断网 / 服务端不通时退避重试，录音和补传互不影响。
 *   串口模式（没配网络时）：同样从卡读文件，按行写 USB 串口，由 tools/serial_capture.py 接收。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { XPORT_SERIAL = 0, XPORT_HTTP } xport_mode_t;

typedef struct {
    xport_mode_t mode;
    bool     online;        /* 最近一次请求服务端成功 */
    bool     auth_error;    /* 服务端返回 401：口令不对 */
    uint32_t sent_chunks;   /* 当前正在上传段的已确认片数 */
    uint32_t retries;       /* 累计重试次数 */
    uint32_t last_send_ms;  /* 最近一片发送耗时 */
} transport_status_t;

void transport_start_task(void);
void transport_get_status(transport_status_t *out);
const char *transport_device_id(void);
