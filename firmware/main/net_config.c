#include "net_config.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif

static const char *TAG = "netcfg";

void netcfg_load(netcfg_t *out)
{
    memset(out, 0, sizeof(*out));
    nvs_handle_t h;
    if (nvs_open("netcfg", NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(*out);
        if (nvs_get_blob(h, "cfg", out, &len) != ESP_OK || len != sizeof(*out)) {
            memset(out, 0, sizeof(*out));
        }
        nvs_close(h);
    }
#ifdef DEFAULT_WIFI_SSID
    if (out->wifi_count == 0 && DEFAULT_WIFI_SSID[0] && strcmp(DEFAULT_WIFI_SSID, "your-ssid") != 0) {
        strlcpy(out->wifi[0].ssid, DEFAULT_WIFI_SSID, sizeof(out->wifi[0].ssid));
        strlcpy(out->wifi[0].pass, DEFAULT_WIFI_PASS, sizeof(out->wifi[0].pass));
        out->wifi_count = 1;
    }
#endif
#ifdef DEFAULT_SERVER_URL
    if (!out->server_url[0] && !strstr(DEFAULT_SERVER_URL, "your-mac")) {
        strlcpy(out->server_url, DEFAULT_SERVER_URL, sizeof(out->server_url));
        strlcpy(out->token, DEFAULT_API_TOKEN, sizeof(out->token));
    }
#endif
    if (out->wifi_count < 0 || out->wifi_count > NETCFG_MAX_WIFI) out->wifi_count = 0;
    ESP_LOGI(TAG, "%d wifi, server=%s", out->wifi_count, out->server_url[0] ? out->server_url : "(none)");
}

bool netcfg_save(const netcfg_t *cfg)
{
    nvs_handle_t h;
    if (nvs_open("netcfg", NVS_READWRITE, &h) != ESP_OK) return false;
    bool ok = nvs_set_blob(h, "cfg", cfg, sizeof(*cfg)) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    return ok;
}

bool netcfg_usable(const netcfg_t *cfg)
{
    return cfg->wifi_count > 0 && cfg->server_url[0];
}
