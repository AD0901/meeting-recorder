#include "transport.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "chunk_ring.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/ip4_addr.h"
#include "mbedtls/base64.h"
#include "mdns.h"
#include "wifi_mgr.h"

static const char *TAG = "xport";

static char s_device_id[13];
static uint32_t s_boot_id;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static transport_status_t s_st;

const char *transport_device_id(void) { return s_device_id; }

void transport_get_status(transport_status_t *out)
{
    portENTER_CRITICAL(&s_lock);
    *out = s_st;
    portEXIT_CRITICAL(&s_lock);
}

#define ST_SET(expr)                 \
    do {                             \
        portENTER_CRITICAL(&s_lock); \
        expr;                        \
        portEXIT_CRITICAL(&s_lock);  \
    } while (0)

static void log_chunk_sent(const chunk_t *c, uint32_t ms)
{
    uint32_t lag_ms = (uint32_t)((esp_timer_get_time() - c->t_us) / 1000);
    ESP_LOGI(TAG, "seq %lu sent %lu B in %lu ms, queued %lu ms, ring %u/%u",
             (unsigned long)c->seq, (unsigned long)c->len, (unsigned long)ms,
             (unsigned long)lag_ms, (unsigned)ring_used() - 1, (unsigned)ring_capacity());
}

/* ================================================================ 串口模式（里程碑 2） */

static unsigned char *s_b64;
#define B64_CAP (CHUNK_MAX_BYTES * 4 / 3 + 8)

static void serial_send(const chunk_t *c)
{
    switch (c->type) {
    case CHUNK_BEGIN:
        printf("\n@@BEGIN session=%lu device_id=%s sample_rate=16000 channels=1 format=pcm_s16le\n",
               (unsigned long)c->session, s_device_id);
        ST_SET(s_st.session = c->session; s_st.sent_chunks = 0; s_st.session_done = false);
        break;
    case CHUNK_AUDIO: {
        int64_t t0 = esp_timer_get_time();
        size_t olen = 0;
        mbedtls_base64_encode(s_b64, B64_CAP, &olen, c->data, c->len);
        uint32_t crc = esp_rom_crc32_le(0, c->data, c->len);
        flockfile(stdout);  /* 整行不能被其他任务的日志插进来 */
        printf("@@CHUNK session=%lu seq=%lu bytes=%lu crc32=%08lx b64=",
               (unsigned long)c->session, (unsigned long)c->seq,
               (unsigned long)c->len, (unsigned long)crc);
        fwrite(s_b64, 1, olen, stdout);
        fputc('\n', stdout);
        fflush(stdout);
        funlockfile(stdout);
        uint32_t ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        ST_SET(s_st.sent_chunks++; s_st.last_seq_sent = c->seq; s_st.last_send_ms = ms);
        log_chunk_sent(c, ms);
        break;
    }
    case CHUNK_MARK:
        printf("@@MARK session=%lu offset_ms=%lu\n", (unsigned long)c->session, (unsigned long)c->seq);
        break;
    case CHUNK_END:
        printf("@@END session=%lu last_seq=%ld\n", (unsigned long)c->session, (long)(int32_t)c->seq);
        fflush(stdout);
        ST_SET(s_st.session_done = true);
        break;
    }
}

/* ================================================================ HTTP 模式（里程碑 3） */

static char s_base[160];        /* 已把 .local 解析成 IP 的服务器地址 */
static esp_http_client_handle_t s_cli;
static char s_resp[256];
static int s_resp_len;

static esp_err_t on_http(esp_http_client_event_t *e)
{
    if (e->event_id == HTTP_EVENT_ON_DATA && s_resp_len + e->data_len < (int)sizeof(s_resp)) {
        memcpy(s_resp + s_resp_len, e->data, e->data_len);
        s_resp_len += e->data_len;
        s_resp[s_resp_len] = 0;
    }
    return ESP_OK;
}

/* http://host.local:8000 → http://192.168.x.x:8000（ESP-IDF 的 DNS 不会查 .local） */
static bool resolve_base(void)
{
    const char *url = net_config()->server_url;
    const char *h = strstr(url, "://");
    h = h ? h + 3 : url;
    const char *end = h + strcspn(h, ":/");
    size_t hl = end - h;
    if (hl > 6 && strncmp(end - 6, ".local", 6) == 0) {
        char host[64];
        snprintf(host, sizeof(host), "%.*s", (int)(hl - 6), h);
        esp_ip4_addr_t addr = { 0 };
        if (mdns_query_a(host, 2000, &addr) != ESP_OK) {
            ESP_LOGW(TAG, "mDNS: %s.local not found", host);
            return false;
        }
        snprintf(s_base, sizeof(s_base), "%.*s" IPSTR "%s", (int)(h - url), url, IP2STR(&addr), end);
        ESP_LOGI(TAG, "server %s -> %s", url, s_base);
    } else {
        strlcpy(s_base, url, sizeof(s_base));
    }
    return true;
}

static void http_reset(void)
{
    if (s_cli) {
        esp_http_client_cleanup(s_cli);
        s_cli = NULL;
    }
}

/* 发一个请求，返回 HTTP 状态码；网络错误返回 -1 */
static int http_req(esp_http_client_method_t method, const char *path, const char *ctype,
                    const void *body, size_t len, const char *crc_hdr)
{
    if (!s_base[0] && !resolve_base()) return -1;
    char url[256];
    snprintf(url, sizeof(url), "%s%s", s_base, path);
    bool reused = s_cli != NULL;
    if (!s_cli) {
        esp_http_client_config_t cfg = {
            .url = url,
            .timeout_ms = 6000,
            .event_handler = on_http,
            .keep_alive_enable = true,
            .buffer_size_tx = 1024,
        };
        s_cli = esp_http_client_init(&cfg);
        if (!s_cli) return -1;
    }
    esp_http_client_set_url(s_cli, url);
    esp_http_client_set_method(s_cli, method);
    const char *tok = net_config()->token;
    if (tok[0]) {
        char auth[80];
        snprintf(auth, sizeof(auth), "Bearer %s", tok);
        esp_http_client_set_header(s_cli, "Authorization", auth);
    }
    if (ctype) esp_http_client_set_header(s_cli, "Content-Type", ctype);
    if (crc_hdr) esp_http_client_set_header(s_cli, "X-CRC32", crc_hdr);
    else esp_http_client_delete_header(s_cli, "X-CRC32");
    esp_http_client_set_post_field(s_cli, body, (int)len);

    s_resp_len = 0;
    s_resp[0] = 0;
    esp_err_t err = esp_http_client_perform(s_cli);
    if (err != ESP_OK && reused) {
        /* 复用的长连接可能已被服务端关掉：换新连接立刻重试一次，不算失败 */
        http_reset();
        return http_req(method, path, ctype, body, len, crc_hdr);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s failed: %s", path, esp_err_to_name(err));
        http_reset();
        return -1;
    }
    return esp_http_client_get_status_code(s_cli);
}

/* 本地会话 → 服务端 session_id */
static uint32_t s_local_session;
static char s_sid[48];
static bool s_session_lost;   /* 服务端说这个会话不存在了：剩下的消息丢掉 */
static bool s_end_force;

typedef enum { XR_OK, XR_RETRY, XR_DROP } result_t;

static result_t classify(int code)
{
    if (code >= 200 && code < 300) {
        ST_SET(s_st.online = true; s_st.auth_error = false);
        return XR_OK;
    }
    if (code < 0 || code >= 500 || code == 408 || code == 429) {
        ST_SET(s_st.online = false);
        return XR_RETRY;
    }
    ST_SET(s_st.online = true; s_st.auth_error = (code == 401));
    if (code == 401) return XR_RETRY;   /* 口令不对：等用户改配置，不丢数据 */
    return XR_DROP;
}

static result_t http_send(const chunk_t *c)
{
    char path[128], json[200];

    if (c->type == CHUNK_BEGIN) {
        snprintf(json, sizeof(json),
                 "{\"device_id\":\"%s\",\"device_session\":\"%08lx-%lu\",\"sample_rate\":16000,"
                 "\"channels\":1,\"format\":\"pcm_s16le\"}",
                 s_device_id, (unsigned long)s_boot_id, (unsigned long)c->session);
        int code = http_req(HTTP_METHOD_POST, "/api/sessions", "application/json", json, strlen(json), NULL);
        result_t r = classify(code);
        if (r == XR_OK) {
            const char *p = strstr(s_resp, "\"session_id\"");
            p = p ? strchr(p + 12, '"') : NULL;
            if (!p) return XR_RETRY;
            p++;
            size_t n = strcspn(p, "\"");
            snprintf(s_sid, sizeof(s_sid), "%.*s", (int)n, p);
            s_local_session = c->session;
            s_session_lost = false;
            s_end_force = false;
            ST_SET(s_st.session = c->session; s_st.sent_chunks = 0; s_st.session_done = false;
                   s_st.retries = 0; strlcpy(s_st.server_session, s_sid, sizeof(s_st.server_session)));
            ESP_LOGI(TAG, "session %lu -> server %s", (unsigned long)c->session, s_sid);
        } else if (r == XR_DROP) {
            ESP_LOGE(TAG, "server refused session (HTTP %d): %s", code, s_resp);
            s_local_session = c->session;
            s_session_lost = true;
            ST_SET(s_st.session = c->session; s_st.session_done = false);
        }
        return r;
    }

    if (c->session != s_local_session || s_session_lost) {
        if (c->type == CHUNK_END) ST_SET(s_st.session_done = true);
        return XR_DROP;
    }

    int code;
    switch (c->type) {
    case CHUNK_AUDIO: {
        char crc[12];
        snprintf(crc, sizeof(crc), "%08lx", (unsigned long)esp_rom_crc32_le(0, c->data, c->len));
        snprintf(path, sizeof(path), "/api/sessions/%s/chunks?seq=%lu", s_sid, (unsigned long)c->seq);
        int64_t t0 = esp_timer_get_time();
        code = http_req(HTTP_METHOD_POST, path, "application/octet-stream", c->data, c->len, crc);
        uint32_t ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        result_t r = classify(code);
        if (r == XR_OK) {
            ST_SET(s_st.sent_chunks++; s_st.last_seq_sent = c->seq; s_st.last_send_ms = ms);
            log_chunk_sent(c, ms);
        } else if (r == XR_DROP) {
            ESP_LOGE(TAG, "seq %lu refused (HTTP %d) %s", (unsigned long)c->seq, code, s_resp);
            if (code == 404) s_session_lost = true;
        }
        return r;
    }
    case CHUNK_MARK:
        snprintf(path, sizeof(path), "/api/sessions/%s/marks", s_sid);
        snprintf(json, sizeof(json), "{\"offset_ms\":%lu}", (unsigned long)c->seq);
        code = http_req(HTTP_METHOD_POST, path, "application/json", json, strlen(json), NULL);
        if (code >= 200 && code < 300) ESP_LOGI(TAG, "mark %lu ms sent", (unsigned long)c->seq);
        return classify(code);
    case CHUNK_END: {
        snprintf(path, sizeof(path), "/api/sessions/%s/end", s_sid);
        snprintf(json, sizeof(json), "{\"last_seq\":%ld,\"force\":%s}", (long)(int32_t)c->seq,
                 s_end_force ? "true" : "false");
        code = http_req(HTTP_METHOD_POST, path, "application/json", json, strlen(json), NULL);
        if (code == 409) {  /* 服务端说缺片（正常不会发生）：下次带 force 结束 */
            ESP_LOGE(TAG, "server reports missing chunks: %s", s_resp);
            s_end_force = true;
            return XR_RETRY;
        }
        result_t r = classify(code);
        if (r != XR_RETRY) {
            ST_SET(s_st.session_done = true);
            ESP_LOGI(TAG, "session %s ended (HTTP %d)", s_sid, code);
        }
        return r;
    }
    default:
        return XR_DROP;
    }
}

/* ================================================================ 任务 */

static void transport_task(void *arg)
{
    bool http = s_st.mode == XPORT_HTTP;
    uint32_t backoff = 500;
    int64_t last_ping = 0;
    int fails = 0;

    while (1) {
        chunk_t *c = ring_peek(pdMS_TO_TICKS(http ? 200 : 500));

        if (!http) {
            if (!c) continue;
            serial_send(c);
            ring_release();
            continue;
        }

        if (!net_wait_connected(0)) {
            ST_SET(s_st.online = false);
            if (s_cli) http_reset();
            net_wait_connected(pdMS_TO_TICKS(1000));
            continue;
        }

        if (!c) {  /* 空闲时每 10 秒探一下服务端，屏幕上显示连没连上 */
            if (esp_timer_get_time() - last_ping > 10 * 1000000LL) {
                last_ping = esp_timer_get_time();
                int code = http_req(HTTP_METHOD_GET, "/api/health", NULL, NULL, 0, NULL);
                classify(code);
                if (code < 0 && ++fails >= 3) { s_base[0] = 0; fails = 0; }  /* 重新解析 .local */
                else if (code >= 0) fails = 0;
            }
            continue;
        }

        result_t r = http_send(c);
        if (r == XR_RETRY) {
            ST_SET(s_st.retries++);
            if (++fails >= 3) { s_base[0] = 0; fails = 0; }  /* Mac 换了 IP 也能找回来 */
            ESP_LOGW(TAG, "retry in %lu ms (ring %u/%u)", (unsigned long)backoff,
                     (unsigned)ring_used(), (unsigned)ring_capacity());
            vTaskDelay(pdMS_TO_TICKS(backoff));
            backoff = backoff * 2 > 4000 ? 4000 : backoff * 2;
            continue;
        }
        fails = 0;
        backoff = 500;
        last_ping = esp_timer_get_time();
        ring_release();
    }
}

void transport_start_task(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_device_id, sizeof(s_device_id), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    s_boot_id = esp_random();
    s_st.session_done = true;
    s_st.mode = netcfg_usable(net_config()) ? XPORT_HTTP : XPORT_SERIAL;
    if (s_st.mode == XPORT_SERIAL) {
        s_b64 = heap_caps_malloc(B64_CAP, MALLOC_CAP_SPIRAM);
        assert(s_b64);
    }
    xTaskCreatePinnedToCore(transport_task, "xport", 8192, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "%s transport ready, device_id=%s",
             s_st.mode == XPORT_HTTP ? "HTTP" : "serial", s_device_id);
}
