/*
 * Wi-Fi 管理：
 *   - 正常模式（STA）：扫描周围网络，在已记住的 Wi-Fi 里挑信号最好的连；掉线后自动重扫重连。
 *     所以换会议室时，只要那里的 Wi-Fi 之前配过，就会自己连上。
 *   - 配网模式（AP）：设备开热点 MeetRec-XXXXXX，手机连上后打开 http://192.168.4.1
 *     填 Wi-Fi 和服务器地址，保存后自动重启。没有任何配置时开机就进入这个模式。
 */
#include "wifi_mgr.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "esp_attr.h"
#include "mdns.h"

static const char *TAG = "wifi";

#define BIT_CONNECTED    BIT0
#define BIT_DISCONNECTED BIT1

static EventGroupHandle_t s_ev;
static netcfg_t s_cfg;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static net_status_t s_st;
static esp_netif_t *s_sta, *s_ap;

/* 配网页用：扫描到的网络名 */
#define MAX_SCAN 16
static char s_scan[MAX_SCAN][33];
static int s_scan_n;

const netcfg_t *net_config(void) { return &s_cfg; }

void net_get_status(net_status_t *out)
{
    portENTER_CRITICAL(&s_lock);
    *out = s_st;
    portEXIT_CRITICAL(&s_lock);
}

static void set_state(net_state_t st, const char *ssid)
{
    portENTER_CRITICAL(&s_lock);
    s_st.state = st;
    if (ssid) strlcpy(s_st.ssid, ssid, sizeof(s_st.ssid));
    if (st != NET_CONNECTED) s_st.ip[0] = 0;
    portEXIT_CRITICAL(&s_lock);
}

bool net_wait_connected(TickType_t wait)
{
    return xEventGroupWaitBits(s_ev, BIT_CONNECTED, pdFALSE, pdFALSE, wait) & BIT_CONNECTED;
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = data;
        ESP_LOGW(TAG, "disconnected (reason %d)", d->reason);
        xEventGroupClearBits(s_ev, BIT_CONNECTED);
        xEventGroupSetBits(s_ev, BIT_DISCONNECTED);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        portENTER_CRITICAL(&s_lock);
        s_st.state = NET_CONNECTED;
        snprintf(s_st.ip, sizeof(s_st.ip), IPSTR, IP2STR(&e->ip_info.ip));
        portEXIT_CRITICAL(&s_lock);
        ESP_LOGI(TAG, "connected to %s, ip %s", s_st.ssid, s_st.ip);
        xEventGroupClearBits(s_ev, BIT_DISCONNECTED);
        xEventGroupSetBits(s_ev, BIT_CONNECTED);
    }
}

/* ---------------------------------------------------------------- STA：挑信号最好的已知网络 */

static int scan_best_known(void)
{
    wifi_scan_config_t sc = { .show_hidden = true };
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) return -1;
    uint16_t n = 32;
    wifi_ap_record_t *recs = calloc(n, sizeof(*recs));
    if (!recs) return -1;
    esp_wifi_scan_get_ap_records(&n, recs);
    int best = -1, best_rssi = -1000;
    for (int i = 0; i < n; i++) {
        for (int k = 0; k < s_cfg.wifi_count; k++) {
            if (strcmp((char *)recs[i].ssid, s_cfg.wifi[k].ssid) == 0 && recs[i].rssi > best_rssi) {
                best = k;
                best_rssi = recs[i].rssi;
            }
        }
    }
    free(recs);
    if (best >= 0) {
        portENTER_CRITICAL(&s_lock);
        s_st.rssi = best_rssi;
        portEXIT_CRITICAL(&s_lock);
    }
    return best;
}

static void sta_task(void *arg)
{
    int fails = 0;
    while (1) {
        set_state(NET_CONNECTING, NULL);
        int k = scan_best_known();
        if (k < 0) {
            ESP_LOGW(TAG, "no known network in range, rescan in 5 s");
            set_state(NET_CONNECTING, "");
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }
        wifi_config_t wc = { 0 };
        strlcpy((char *)wc.sta.ssid, s_cfg.wifi[k].ssid, sizeof(wc.sta.ssid));
        strlcpy((char *)wc.sta.password, s_cfg.wifi[k].pass, sizeof(wc.sta.password));
        wc.sta.threshold.authmode = s_cfg.wifi[k].pass[0] ? WIFI_AUTH_WEP : WIFI_AUTH_OPEN;
        wc.sta.pmf_cfg.capable = true;
        esp_wifi_set_config(WIFI_IF_STA, &wc);
        set_state(NET_CONNECTING, s_cfg.wifi[k].ssid);
        ESP_LOGI(TAG, "connecting to %s", s_cfg.wifi[k].ssid);
        xEventGroupClearBits(s_ev, BIT_DISCONNECTED);
        esp_wifi_connect();

        EventBits_t b = xEventGroupWaitBits(s_ev, BIT_CONNECTED | BIT_DISCONNECTED, pdFALSE, pdFALSE,
                                            pdMS_TO_TICKS(15000));
        if (b & BIT_CONNECTED) {
            fails = 0;
            /* 连着就一直等，直到掉线 */
            xEventGroupWaitBits(s_ev, BIT_DISCONNECTED, pdFALSE, pdFALSE, portMAX_DELAY);
            set_state(NET_CONNECTING, NULL);
        } else {
            esp_wifi_disconnect();
            fails++;
        }
        vTaskDelay(pdMS_TO_TICKS(fails > 3 ? 5000 : 1000));
    }
}

/* ---------------------------------------------------------------- 配网网页 */

static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++, o++) {
        if (*s == '+') {
            *o = ' ';
        } else if (*s == '%' && s[1] && s[2]) {
            char hex[3] = { s[1], s[2], 0 };
            *o = (char)strtol(hex, NULL, 16);
            s += 2;
        } else {
            *o = *s;
        }
    }
    *o = 0;
}

static void html_escape_append(char *dst, size_t cap, const char *src)
{
    size_t n = strlen(dst);
    for (; *src && n + 7 < cap; src++) {
        const char *rep = NULL;
        switch (*src) {
        case '<': rep = "&lt;"; break;
        case '>': rep = "&gt;"; break;
        case '&': rep = "&amp;"; break;
        case '"': rep = "&quot;"; break;
        }
        if (rep) { strcpy(dst + n, rep); n += strlen(rep); }
        else dst[n++] = *src;
    }
    dst[n] = 0;
}

static const char PAGE_HEAD[] =
    "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'><title>录音器配网</title><style>"
    "body{font:16px -apple-system,'PingFang SC',sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#1d2129}"
    "h1{font-size:20px}fieldset{border:0;background:#fff;border-radius:12px;margin:0 0 12px;padding:12px}"
    "legend{font-weight:600;padding:0}label{display:block;font-size:13px;color:#6b7280;margin-top:8px}"
    "input{width:100%;box-sizing:border-box;font-size:16px;padding:10px;border:1px solid #d1d5db;border-radius:8px}"
    "button{width:100%;font-size:17px;padding:14px;border:0;border-radius:10px;background:#2563eb;color:#fff}"
    "small{color:#6b7280}</style></head><body><h1>会议录音器 · 网络设置</h1>"
    "<form method=post action=/save>";

static esp_err_t page_get(httpd_req_t *req)
{
    char *buf = calloc(1, 8192);
    if (!buf) return ESP_FAIL;
    strcpy(buf, PAGE_HEAD);
    strcat(buf, "<datalist id=nets>");
    for (int i = 0; i < s_scan_n; i++) {
        strcat(buf, "<option value=\"");
        html_escape_append(buf, 8192, s_scan[i]);
        strcat(buf, "\">");
    }
    strcat(buf, "</datalist>");
    for (int i = 0; i < NETCFG_MAX_WIFI; i++) {
        char line[160];
        snprintf(line, sizeof(line), "<fieldset><legend>Wi-Fi %d%s</legend><label>网络名称</label>"
                 "<input name=s%d list=nets autocapitalize=off value=\"", i + 1, i == 0 ? "" : "（可选）", i);
        strcat(buf, line);
        if (i < s_cfg.wifi_count) html_escape_append(buf, 8192, s_cfg.wifi[i].ssid);
        snprintf(line, sizeof(line), "\"><label>密码%s</label><input name=p%d type=password placeholder=\"%s\">"
                 "</fieldset>", "", i, (i < s_cfg.wifi_count && s_cfg.wifi[i].pass[0]) ? "已保存，不改请留空" : "");
        strcat(buf, line);
    }
    strcat(buf, "<fieldset><legend>服务器</legend><label>地址</label>"
                "<input name=url required autocapitalize=off placeholder='http://服务器IP:8000' value=\"");
    html_escape_append(buf, 8192, s_cfg.server_url);
    strcat(buf, "\"><label>口令（服务端 API_TOKEN，没有就留空）</label><input name=tok autocapitalize=off value=\"");
    html_escape_append(buf, 8192, s_cfg.token);
    strcat(buf, "\"><small>云服务器填 http://公网IP:8000；用自己 Mac 当服务端时填 http://电脑名.local:8000</small></fieldset>"
                "<button>保存并重启</button></form></body></html>");
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_sendstr(req, buf);
    free(buf);
    return ESP_OK;
}

/* 从 a=1&b=2 里取一个字段（已解码） */
static void form_get(char *body, const char *key, char *out, size_t cap)
{
    out[0] = 0;
    char *copy = strdup(body);
    if (!copy) return;
    char *save = NULL;
    for (char *kv = strtok_r(copy, "&", &save); kv; kv = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(kv, '=');
        if (!eq) continue;
        *eq = 0;
        if (strcmp(kv, key) == 0) {
            url_decode(eq + 1);
            strlcpy(out, eq + 1, cap);
            break;
        }
    }
    free(copy);
}

static void restart_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t page_save(httpd_req_t *req)
{
    int len = req->content_len;
    if (len <= 0 || len > 4096) return ESP_FAIL;
    char *body = calloc(1, len + 1);
    if (!body) return ESP_FAIL;
    int got = 0;
    while (got < len) {
        int r = httpd_req_recv(req, body + got, len - got);
        if (r <= 0) { free(body); return ESP_FAIL; }
        got += r;
    }

    netcfg_t n = { 0 };
    for (int i = 0; i < NETCFG_MAX_WIFI; i++) {
        char key[4], ssid[33], pass[65];
        snprintf(key, sizeof(key), "s%d", i);
        form_get(body, key, ssid, sizeof(ssid));
        snprintf(key, sizeof(key), "p%d", i);
        form_get(body, key, pass, sizeof(pass));
        if (!ssid[0]) continue;
        if (!pass[0]) {  /* 留空 = 沿用已保存的密码 */
            for (int k = 0; k < s_cfg.wifi_count; k++) {
                if (strcmp(s_cfg.wifi[k].ssid, ssid) == 0) strlcpy(pass, s_cfg.wifi[k].pass, sizeof(pass));
            }
        }
        strlcpy(n.wifi[n.wifi_count].ssid, ssid, sizeof(ssid));
        strlcpy(n.wifi[n.wifi_count].pass, pass, sizeof(pass));
        n.wifi_count++;
    }
    form_get(body, "url", n.server_url, sizeof(n.server_url));
    form_get(body, "tok", n.token, sizeof(n.token));
    free(body);
    /* 去掉末尾的 / */
    size_t ul = strlen(n.server_url);
    while (ul && n.server_url[ul - 1] == '/') n.server_url[--ul] = 0;

    bool ok = netcfg_save(&n);
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_sendstr(req, ok
        ? "<meta charset=utf-8><meta name=viewport content='width=device-width'>"
          "<h2 style='font-family:sans-serif'>已保存，设备正在重启 …</h2><p>手机可以断开这个热点了。</p>"
        : "<meta charset=utf-8><h2>保存失败</h2>");
    ESP_LOGI(TAG, "config saved (%d wifi, server %s), restarting", n.wifi_count, n.server_url);
    if (ok) xTaskCreate(restart_task, "restart", 2048, NULL, 5, NULL);
    return ESP_OK;
}

static void start_portal(void)
{
    httpd_handle_t srv = NULL;
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192;
    ESP_ERROR_CHECK(httpd_start(&srv, &cfg));
    httpd_uri_t get = { .uri = "/", .method = HTTP_GET, .handler = page_get };
    httpd_uri_t post = { .uri = "/save", .method = HTTP_POST, .handler = page_save };
    httpd_register_uri_handler(srv, &get);
    httpd_register_uri_handler(srv, &post);
}

/* ---------------------------------------------------------------- 启动 */

static void common_init(void)
{
    s_ev = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_sta = esp_netif_create_default_wifi_sta();
    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);

    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_st.ap_ssid, sizeof(s_st.ap_ssid), "MeetRec-%02X%02X%02X", mac[3], mac[4], mac[5]);

    /* mDNS：让设备能用 xxx.local 找到 Mac 上的服务端 */
    if (mdns_init() == ESP_OK) {
        char host[32];
        snprintf(host, sizeof(host), "meetrec-%02x%02x%02x", mac[3], mac[4], mac[5]);
        mdns_hostname_set(host);
    }
}

static void enter_setup(void)
{
    s_ap = esp_netif_create_default_wifi_ap();
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* 先扫一遍，配网页里可以直接选网络名 */
    wifi_scan_config_t sc = { 0 };
    if (esp_wifi_scan_start(&sc, true) == ESP_OK) {
        uint16_t n = 24;
        wifi_ap_record_t *recs = calloc(n, sizeof(*recs));
        if (recs) {
            esp_wifi_scan_get_ap_records(&n, recs);
            for (int i = 0; i < n && s_scan_n < MAX_SCAN; i++) {
                if (!recs[i].ssid[0]) continue;
                bool dup = false;
                for (int k = 0; k < s_scan_n; k++) dup |= strcmp(s_scan[k], (char *)recs[i].ssid) == 0;
                if (!dup) strlcpy(s_scan[s_scan_n++], (char *)recs[i].ssid, 33);
            }
            free(recs);
        }
    }

    wifi_config_t ap = { 0 };
    strlcpy((char *)ap.ap.ssid, s_st.ap_ssid, sizeof(ap.ap.ssid));
    ap.ap.ssid_len = strlen(s_st.ap_ssid);
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    start_portal();
    set_state(NET_SETUP, NULL);
    ESP_LOGI(TAG, "setup mode: join %s, open http://192.168.4.1", s_st.ap_ssid);
}

void net_start(bool force_setup)
{
    netcfg_load(&s_cfg);
    common_init();
    if (force_setup || s_cfg.wifi_count == 0) {
        enter_setup();
        return;
    }
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);  /* 上传时延更稳；耗电多一点 */
    xTaskCreatePinnedToCore(sta_task, "wifi_sta", 4096, NULL, 4, NULL, 0);
}

/* 运行中进入配网：重启后进 AP 模式（避免 STA/AP 状态混在一起） */
static RTC_NOINIT_ATTR uint32_t s_setup_magic;
#define SETUP_MAGIC 0x5E7C0F16u

void net_request_setup(void)
{
    s_setup_magic = SETUP_MAGIC;
    esp_restart();
}

bool net_setup_requested(void)
{
    bool r = esp_reset_reason() == ESP_RST_SW && s_setup_magic == SETUP_MAGIC;
    s_setup_magic = 0;
    return r;
}
