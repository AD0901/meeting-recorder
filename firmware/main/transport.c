#include "transport.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "chunk_ring.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/ip4_addr.h"
#include "mbedtls/base64.h"
#include "mdns.h"
#include "sd_store.h"
#include "wifi_mgr.h"

static const char *TAG = "xport";

static char s_device_id[13];
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

static uint8_t *s_pcm_buf;   /* PSRAM，一片音频的读文件缓冲 */

static void pcm_path_of(char *out, size_t n, uint32_t id)
{
    snprintf(out, n, "/sdcard/rec/%lu.pcm", (unsigned long)id);
}

/* ================================================================ 串口模式 */

static unsigned char *s_b64;
#define B64_CAP (CHUNK_MAX_BYTES * 4 / 3 + 8)

/* ================================================================ HTTP 工具 */

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
    return XR_DROP;                     /* 其他 4xx（如网页端已删除该会话）：放弃这段 */
}

/* ================================================================ HTTP 上传一段 */

static bool s_end_force;

/* 确保有云端 session_id；device_session 去重键保证重发不会建重复会话 */
static result_t ensure_session(rec_meta_t *m)
{
    if (m->session_id[0]) return XR_OK;
    char json[220];
    snprintf(json, sizeof(json),
             "{\"device_id\":\"%s\",\"device_session\":\"sd-%lu\",\"sample_rate\":16000,"
             "\"channels\":1,\"format\":\"pcm_s16le\"}",
             s_device_id, (unsigned long)m->id);
    int code = http_req(HTTP_METHOD_POST, "/api/sessions", "application/json", json, strlen(json), NULL);
    result_t r = classify(code);
    if (r != XR_OK) return r;

    const char *p = strstr(s_resp, "\"session_id\"");
    p = p ? strchr(p + 12, '"') : NULL;
    if (!p) return XR_RETRY;
    p++;
    size_t n = strcspn(p, "\"");
    char sid[48];
    snprintf(sid, sizeof(sid), "%.*s", (int)n, p);
    sd_store_set_session(sid);
    strlcpy(m->session_id, sid, sizeof(m->session_id));
    s_end_force = false;
    ESP_LOGI(TAG, "rec %lu -> server %s", (unsigned long)m->id, sid);
    return XR_OK;
}

static result_t upload_http(rec_meta_t *m)
{
    result_t r = ensure_session(m);
    if (r != XR_OK) return r;

    char path[128], json[200];
    pcm_path_of(path, sizeof(path), m->id);
    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGE(TAG, "rec %lu pcm missing, drop", (unsigned long)m->id);
        return XR_DROP;
    }

    while (m->next_seq < m->total_chunks) {
        fseek(f, (long)m->next_seq * CHUNK_MAX_BYTES, SEEK_SET);
        size_t len = fread(s_pcm_buf, 1, CHUNK_MAX_BYTES, f);
        if (len == 0) break;
        char crc[12];
        snprintf(crc, sizeof(crc), "%08lx", (unsigned long)esp_rom_crc32_le(0, s_pcm_buf, len));
        snprintf(path, sizeof(path), "/api/sessions/%s/chunks?seq=%lu", m->session_id,
                 (unsigned long)m->next_seq);
        int64_t t0 = esp_timer_get_time();
        int code = http_req(HTTP_METHOD_POST, path, "application/octet-stream", s_pcm_buf, len, crc);
        uint32_t ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        r = classify(code);
        if (r != XR_OK) {
            fclose(f);
            if (r == XR_DROP) ESP_LOGE(TAG, "seq %lu refused (HTTP %d) %s",
                                       (unsigned long)m->next_seq, code, s_resp);
            return r;
        }
        m->next_seq++;
        sd_store_progress(m->next_seq);
        ST_SET(s_st.sent_chunks = m->next_seq; s_st.last_send_ms = ms);
        if (m->next_seq % 10 == 0) {
            ESP_LOGI(TAG, "rec %lu %lu/%lu chunks, %lu ms/chunk", (unsigned long)m->id,
                     (unsigned long)m->next_seq, (unsigned long)m->total_chunks, (unsigned long)ms);
        }
    }
    fclose(f);

    /* 打点（服务端按 offset 去重，重发安全） */
    for (uint32_t i = 0; i < m->mark_count; i++) {
        snprintf(path, sizeof(path), "/api/sessions/%s/marks", m->session_id);
        snprintf(json, sizeof(json), "{\"offset_ms\":%lu}", (unsigned long)m->marks[i]);
        int code = http_req(HTTP_METHOD_POST, path, "application/json", json, strlen(json), NULL);
        r = classify(code);
        if (r != XR_OK) return r;
    }

    snprintf(path, sizeof(path), "/api/sessions/%s/end", m->session_id);
    snprintf(json, sizeof(json), "{\"last_seq\":%ld,\"force\":%s}", (long)(int32_t)(m->total_chunks - 1),
             s_end_force ? "true" : "false");
    int code = http_req(HTTP_METHOD_POST, path, "application/json", json, strlen(json), NULL);
    if (code == 409) {   /* 服务端校验缺片：补静音也要结束（正常不会发生） */
        ESP_LOGE(TAG, "server reports missing chunks: %s", s_resp);
        s_end_force = true;
        return XR_RETRY;
    }
    r = classify(code);
    if (r == XR_OK) ESP_LOGI(TAG, "rec %lu END confirmed", (unsigned long)m->id);
    return r;
}

/* ================================================================ 串口上传一段 */

static result_t upload_serial(rec_meta_t *m)
{
    printf("\n@@BEGIN session=%lu device_id=%s sample_rate=16000 channels=1 format=pcm_s16le\n",
           (unsigned long)m->id, s_device_id);

    char path[48];
    pcm_path_of(path, sizeof(path), m->id);
    FILE *f = fopen(path, "rb");
    if (!f) return XR_DROP;

    while (m->next_seq < m->total_chunks) {
        fseek(f, (long)m->next_seq * CHUNK_MAX_BYTES, SEEK_SET);
        size_t len = fread(s_pcm_buf, 1, CHUNK_MAX_BYTES, f);
        if (len == 0) break;
        size_t olen = 0;
        mbedtls_base64_encode(s_b64, B64_CAP, &olen, s_pcm_buf, len);
        uint32_t crc = esp_rom_crc32_le(0, s_pcm_buf, len);
        flockfile(stdout);   /* 整行不能被其他任务的日志插进来 */
        printf("@@CHUNK session=%lu seq=%lu bytes=%lu crc32=%08lx b64=",
               (unsigned long)m->id, (unsigned long)m->next_seq, (unsigned long)len, (unsigned long)crc);
        fwrite(s_b64, 1, olen, stdout);
        fputc('\n', stdout);
        fflush(stdout);
        funlockfile(stdout);
        m->next_seq++;
        ST_SET(s_st.sent_chunks = m->next_seq);
    }
    fclose(f);

    for (uint32_t i = 0; i < m->mark_count; i++) {
        printf("@@MARK session=%lu offset_ms=%lu\n", (unsigned long)m->id, (unsigned long)m->marks[i]);
    }
    printf("@@END session=%lu last_seq=%ld\n", (unsigned long)m->id, (long)(int32_t)(m->total_chunks - 1));
    fflush(stdout);
    return XR_OK;
}

/* ================================================================ 任务 */

static void transport_task(void *arg)
{
    bool http = s_st.mode == XPORT_HTTP;
    uint32_t backoff = 500;
    int64_t last_ping = 0;
    int fails = 0;

    while (1) {
        rec_meta_t m;
        if (!sd_store_claim_pending(&m)) {
            if (http) {
                if (!net_wait_connected(0)) {
                    ST_SET(s_st.online = false);
                } else if (esp_timer_get_time() - last_ping > 10 * 1000000LL) {
                    /* 空闲时每 10 秒探一下服务端，屏幕上显示连没连上 */
                    last_ping = esp_timer_get_time();
                    int code = http_req(HTTP_METHOD_GET, "/api/health", NULL, NULL, 0, NULL);
                    classify(code);
                    if (code < 0 && ++fails >= 3) { s_base[0] = 0; fails = 0; }
                    else if (code >= 0) fails = 0;
                }
            }
            vTaskDelay(pdMS_TO_TICKS(300));
            continue;
        }

        if (http && !net_wait_connected(0)) {
            ST_SET(s_st.online = false);
            sd_store_release();
            net_wait_connected(pdMS_TO_TICKS(1000));
            continue;
        }

        ST_SET(s_st.sent_chunks = m.next_seq);
        result_t r = http ? upload_http(&m) : upload_serial(&m);

        if (r == XR_OK) {
            sd_store_finish();   /* 云端收齐，删卡上文件 */
            backoff = 500;
            fails = 0;
        } else if (r == XR_DROP) {
            ESP_LOGE(TAG, "rec %lu dropped by server, card file removed", (unsigned long)m.id);
            sd_store_finish();
            backoff = 500;
            fails = 0;
        } else {
            sd_store_release();  /* 保存断点，稍后重试 */
            ST_SET(s_st.retries++);
            if (++fails >= 3) { s_base[0] = 0; fails = 0; }
            ESP_LOGW(TAG, "rec %lu retry in %lu ms (next_seq %lu/%lu)", (unsigned long)m.id,
                     (unsigned long)backoff, (unsigned long)m.next_seq, (unsigned long)m.total_chunks);
            vTaskDelay(pdMS_TO_TICKS(backoff));
            backoff = backoff * 2 > 4000 ? 4000 : backoff * 2;
        }
        last_ping = esp_timer_get_time();
    }
}

void transport_start_task(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_device_id, sizeof(s_device_id), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    s_st.mode = netcfg_usable(net_config()) ? XPORT_HTTP : XPORT_SERIAL;
    s_pcm_buf = heap_caps_malloc(CHUNK_MAX_BYTES, MALLOC_CAP_SPIRAM);
    assert(s_pcm_buf);
    if (s_st.mode == XPORT_SERIAL) {
        s_b64 = heap_caps_malloc(B64_CAP, MALLOC_CAP_SPIRAM);
        assert(s_b64);
    }
    xTaskCreatePinnedToCore(transport_task, "xport", 8192, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "%s transport ready, device_id=%s",
             s_st.mode == XPORT_HTTP ? "HTTP" : "serial", s_device_id);
}
