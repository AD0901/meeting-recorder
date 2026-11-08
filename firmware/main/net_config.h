/*
 * 网络配置：最多记住 5 个 Wi-Fi（换会议室不用重新配），外加服务器地址和口令。
 * 存在 NVS（命名空间 "netcfg"）；为空时用 secrets.h 里的默认值（如果有）。
 */
#pragma once

#include <stdbool.h>

#define NETCFG_MAX_WIFI 5

typedef struct {
    char ssid[33];
    char pass[65];
} netcfg_wifi_t;

typedef struct {
    netcfg_wifi_t wifi[NETCFG_MAX_WIFI];
    int  wifi_count;
    char server_url[128];   /* 例如 http://gaodeMacBook-Air.local:8000 */
    char token[65];         /* 服务端 API_TOKEN，可空 */
} netcfg_t;

void netcfg_load(netcfg_t *out);
bool netcfg_save(const netcfg_t *cfg);
bool netcfg_usable(const netcfg_t *cfg);   /* 至少一个 Wi-Fi + 服务器地址 */
