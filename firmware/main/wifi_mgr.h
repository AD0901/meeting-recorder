#pragma once

#include <stdbool.h>

#include "freertos/FreeRTOS.h"
#include "net_config.h"

typedef enum {
    NET_OFF = 0,
    NET_CONNECTING,   /* 正在找 / 连 Wi-Fi（ssid 为空 = 附近没有已知网络） */
    NET_CONNECTED,
    NET_SETUP,        /* 配网模式：设备开热点 ap_ssid */
} net_state_t;

typedef struct {
    net_state_t state;
    char ssid[33];
    char ip[16];
    int  rssi;
    char ap_ssid[33];
} net_status_t;

void net_start(bool force_setup);
void net_get_status(net_status_t *out);
bool net_wait_connected(TickType_t wait);
const netcfg_t *net_config(void);

void net_request_setup(void);     /* 重启进入配网模式 */
bool net_setup_requested(void);   /* 本次启动是否由 net_request_setup 触发（读一次就清掉） */
