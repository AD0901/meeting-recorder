#include "sd_store.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "cJSON.h"
#include "chunk_ring.h"
#include "driver/sdmmc_host.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "sdmmc_cmd.h"

static const char *TAG = "sd";

/* SDMMC 4 线，引脚见 docs/hardware.md */
#define PIN_SD_CLK 16
#define PIN_SD_CMD 15
#define PIN_SD_D0  17
#define PIN_SD_D1  18
#define PIN_SD_D2  13
#define PIN_SD_D3  14

#define REC_DIR "/sdcard/rec"

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_card_ok;
static bool s_write_error;
static uint32_t s_next_id = 1;      /* NVS 持久计数，保证跨重启唯一 */
static uint32_t s_pending;          /* 待上传段数 */

/* 当前正在写的录音（writer 任务独占） */
static FILE *s_cur_file;
static rec_meta_t s_cur;
static uint32_t s_cur_bytes;
static bool s_recording;

/* 待确认段（writer END 时设置，UI save/discard 时清除） */
static bool s_await_confirm;
static rec_meta_t s_confirm;

/* 最近 Save 的段（UI 用来显示上传进度和完成页） */
static bool s_saved_active;
static uint32_t s_saved_id;
static uint32_t s_saved_total;
static uint32_t s_saved_duration_s;
static uint32_t s_saved_marks;

/* 上传任务占用的一段 */
static bool s_claimed;
static rec_meta_t s_claim;

/* ---------------- 路径 / meta 读写 ---------------- */

static void pcm_path(char *out, size_t n, uint32_t id) { snprintf(out, n, REC_DIR "/%lu.pcm", (unsigned long)id); }
static void meta_path(char *out, size_t n, uint32_t id) { snprintf(out, n, REC_DIR "/%lu.json", (unsigned long)id); }

static const char *status_str(rec_status_t s)
{
    return s == REC_RECORDING ? "recording" : s == REC_CONFIRM ? "confirm" : "pending";
}

static bool meta_write(const rec_meta_t *m)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "status", status_str(m->status));
    cJSON_AddNumberToObject(j, "duration_ms", m->duration_ms);
    cJSON_AddNumberToObject(j, "total_chunks", m->total_chunks);
    cJSON_AddNumberToObject(j, "chunk_bytes", m->chunk_bytes);
    cJSON_AddStringToObject(j, "session_id", m->session_id);
    cJSON_AddNumberToObject(j, "next_seq", m->next_seq);
    cJSON *marks = cJSON_AddArrayToObject(j, "marks");
    for (uint32_t i = 0; i < m->mark_count; i++) cJSON_AddItemToArray(marks, cJSON_CreateNumber(m->marks[i]));
    char *txt = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (!txt) return false;

    char path[48], tmp[52];
    meta_path(path, sizeof(path), m->id);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    bool ok = false;
    if (f) {
        ok = fputs(txt, f) >= 0;
        if (fclose(f) != 0) ok = false;
        /* FATFS 的 f_rename 不覆盖已存在的目标文件（返回 FR_EXIST）：
           更新已有 meta 时先删目标再改名（首次创建仍是原子 rename） */
        if (ok && rename(tmp, path) != 0) {
            remove(path);
            ok = rename(tmp, path) == 0;
        }
    }
    free(txt);
    if (!ok) ESP_LOGE(TAG, "meta write failed: %s", path);
    return ok;
}

static bool meta_read(uint32_t id, rec_meta_t *m)
{
    char path[48];
    meta_path(path, sizeof(path), id);
    FILE *f = fopen(path, "r");
    if (!f) return false;
    char buf[1024];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    cJSON *j = cJSON_Parse(buf);
    if (!j) return false;

    memset(m, 0, sizeof(*m));
    m->id = id;
    m->status = REC_PENDING;
    cJSON *v;
    if ((v = cJSON_GetObjectItem(j, "status")) && cJSON_IsString(v)) {
        if (!strcmp(v->valuestring, "recording")) m->status = REC_RECORDING;
        else if (!strcmp(v->valuestring, "confirm")) m->status = REC_CONFIRM;
    }
    if ((v = cJSON_GetObjectItem(j, "duration_ms"))) m->duration_ms = v->valuedouble;
    if ((v = cJSON_GetObjectItem(j, "total_chunks"))) m->total_chunks = v->valuedouble;
    m->chunk_bytes = 32000;   /* 旧 meta 没有该字段时按 1 秒片续传 */
    if ((v = cJSON_GetObjectItem(j, "chunk_bytes")) && v->valuedouble > 0) m->chunk_bytes = v->valuedouble;
    if ((v = cJSON_GetObjectItem(j, "next_seq"))) m->next_seq = v->valuedouble;
    if ((v = cJSON_GetObjectItem(j, "session_id")) && cJSON_IsString(v)) {
        strlcpy(m->session_id, v->valuestring, sizeof(m->session_id));
    }
    cJSON *marks = cJSON_GetObjectItem(j, "marks");
    cJSON *it;
    cJSON_ArrayForEach(it, marks) {
        if (m->mark_count < SD_MAX_MARKS) m->marks[m->mark_count++] = it->valuedouble;
    }
    cJSON_Delete(j);
    return true;
}

static void delete_rec(uint32_t id)
{
    char path[48];
    pcm_path(path, sizeof(path), id);
    remove(path);
    meta_path(path, sizeof(path), id);
    remove(path);
}

/* ---------------- 状态汇总 ---------------- */

void sd_store_get_status(sd_status_t *out)
{
    portENTER_CRITICAL(&s_lock);
    out->card_ok = s_card_ok;
    out->write_error = s_write_error;
    out->await_confirm = s_await_confirm;
    out->confirm_duration_s = s_confirm.duration_ms / 1000;
    out->confirm_marks = s_confirm.mark_count;
    out->pending = s_pending;
    out->saved_active = s_saved_active;
    out->saved_total = s_saved_total;
    out->saved_duration_s = s_saved_duration_s;
    out->saved_marks = s_saved_marks;
    portEXIT_CRITICAL(&s_lock);
}

/* ---------------- Save / Discard ---------------- */

void sd_store_save(void)
{
    portENTER_CRITICAL(&s_lock);
    bool go = s_await_confirm;
    rec_meta_t m = s_confirm;
    s_await_confirm = false;
    portEXIT_CRITICAL(&s_lock);
    if (!go) return;

    m.status = REC_PENDING;
    meta_write(&m);
    portENTER_CRITICAL(&s_lock);
    s_pending++;
    s_saved_active = true;
    s_saved_id = m.id;
    s_saved_total = m.total_chunks;
    s_saved_duration_s = m.duration_ms / 1000;
    s_saved_marks = m.mark_count;
    portEXIT_CRITICAL(&s_lock);
    ESP_LOGI(TAG, "rec %lu saved -> pending (%lu chunks)", (unsigned long)m.id, (unsigned long)m.total_chunks);
}

void sd_store_discard(void)
{
    portENTER_CRITICAL(&s_lock);
    bool go = s_await_confirm;
    uint32_t id = s_confirm.id;
    s_await_confirm = false;
    portEXIT_CRITICAL(&s_lock);
    if (!go) return;
    delete_rec(id);
    ESP_LOGI(TAG, "rec %lu discarded", (unsigned long)id);
}

/* ---------------- 上传任务接口 ---------------- */

bool sd_store_claim_pending(rec_meta_t *out)
{
    if (!s_card_ok) return false;
    portENTER_CRITICAL(&s_lock);
    if (s_claimed) {
        portEXIT_CRITICAL(&s_lock);
        return false;
    }
    s_claimed = true;   /* 先占位，找不到再放开 */
    portEXIT_CRITICAL(&s_lock);

    bool found = false;
    DIR *d = opendir(REC_DIR);
    if (d) {
        struct dirent *e;
        uint32_t best = UINT32_MAX;
        while ((e = readdir(d))) {
            char *end;
            uint32_t id = strtoul(e->d_name, &end, 10);
            if (end == e->d_name || strcmp(end, ".json") || id >= best) continue;
            rec_meta_t m;
            if (meta_read(id, &m) && m.status == REC_PENDING) {
                s_claim = m;
                best = id;
                found = true;
            }
        }
        closedir(d);
    }
    if (!found) {
        portENTER_CRITICAL(&s_lock);
        s_claimed = false;
        portEXIT_CRITICAL(&s_lock);
        return false;
    }
    *out = s_claim;
    ESP_LOGI(TAG, "rec %lu claimed: %lu chunks from seq %lu", (unsigned long)out->id,
             (unsigned long)out->total_chunks, (unsigned long)out->next_seq);
    return true;
}

void sd_store_set_session(const char *session_id)
{
    portENTER_CRITICAL(&s_lock);
    strlcpy(s_claim.session_id, session_id, sizeof(s_claim.session_id));
    rec_meta_t m = s_claim;
    portEXIT_CRITICAL(&s_lock);
    meta_write(&m);
}

void sd_store_progress(uint32_t next_seq)
{
    portENTER_CRITICAL(&s_lock);
    s_claim.next_seq = next_seq;
    rec_meta_t m = s_claim;
    portEXIT_CRITICAL(&s_lock);
    if (next_seq % 8 == 0) meta_write(&m);   /* 每 8 片落一次断点，减少写磨损 */
}

void sd_store_release(void)
{
    portENTER_CRITICAL(&s_lock);
    rec_meta_t m = s_claim;
    bool was = s_claimed;
    s_claimed = false;
    portEXIT_CRITICAL(&s_lock);
    if (was) meta_write(&m);
}

void sd_store_finish(void)
{
    portENTER_CRITICAL(&s_lock);
    uint32_t id = s_claim.id;
    s_claimed = false;
    if (s_pending) s_pending--;
    if (s_saved_active && s_saved_id == id) s_saved_active = false;
    portEXIT_CRITICAL(&s_lock);
    delete_rec(id);
    ESP_LOGI(TAG, "rec %lu uploaded, card file removed", (unsigned long)id);
}

/* ---------------- 写卡任务（ring 唯一消费者） ---------------- */

static void writer_task(void *arg)
{
    while (1) {
        chunk_t *c = ring_peek(pdMS_TO_TICKS(300));
        if (!c) continue;

        if (!s_card_ok) {   /* 无卡：消费并丢弃，防止 ring 堵死（UI 已禁止录音） */
            ring_release();
            continue;
        }

        switch (c->type) {
        case CHUNK_BEGIN: {
            if (s_cur_file) { fclose(s_cur_file); s_cur_file = NULL; }
            memset(&s_cur, 0, sizeof(s_cur));
            s_cur.id = s_next_id;
            s_cur.status = REC_RECORDING;
            s_cur.chunk_bytes = SD_UPLOAD_CHUNK_BYTES;
            s_cur_bytes = 0;
            s_recording = true;
            s_write_error = false;

            nvs_handle_t nvs;
            if (nvs_open("rec", NVS_READWRITE, &nvs) == ESP_OK) {
                s_next_id++;
                nvs_set_u32(nvs, "next_id", s_next_id);
                nvs_commit(nvs);
                nvs_close(nvs);
            }
            char path[48];
            pcm_path(path, sizeof(path), s_cur.id);
            s_cur_file = fopen(path, "wb");
            if (!s_cur_file) {
                ESP_LOGE(TAG, "open %s failed", path);
                s_write_error = true;
                s_recording = false;
            } else {
                meta_write(&s_cur);
                ESP_LOGI(TAG, "rec %lu begin (capture session %lu)", (unsigned long)s_cur.id,
                         (unsigned long)c->session);
            }
            break;
        }
        case CHUNK_AUDIO:
            if (s_recording && s_cur_file) {
                if (fwrite(c->data, 1, c->len, s_cur_file) != c->len) {
                    ESP_LOGE(TAG, "write failed at seq %lu", (unsigned long)c->seq);
                    s_write_error = true;
                } else {
                    s_cur_bytes += c->len;
                    if (c->seq % 10 == 9) fsync(fileno(s_cur_file));   /* 每 10 秒落盘 */
                }
            }
            break;
        case CHUNK_MARK:
            if (s_recording && s_cur.mark_count < SD_MAX_MARKS) {
                s_cur.marks[s_cur.mark_count++] = c->seq;
                meta_write(&s_cur);   /* 打点也落 meta，断电不丢 */
                ESP_LOGI(TAG, "rec %lu mark %lu ms", (unsigned long)s_cur.id, (unsigned long)c->seq);
            }
            break;
        case CHUNK_END:
            if (s_recording) {
                if (s_cur_file) {
                    fsync(fileno(s_cur_file));
                    fclose(s_cur_file);
                    s_cur_file = NULL;
                }
                s_recording = false;
                s_cur.duration_ms = s_cur_bytes / 32;   /* 32 B/ms @16kHz16bit mono */
                s_cur.total_chunks = (s_cur_bytes + s_cur.chunk_bytes - 1) / s_cur.chunk_bytes;
                s_cur.status = REC_CONFIRM;
                if (s_cur.total_chunks == 0) {
                    delete_rec(s_cur.id);   /* 空录音直接丢 */
                } else {
                    meta_write(&s_cur);
                    portENTER_CRITICAL(&s_lock);
                    s_confirm = s_cur;
                    s_await_confirm = true;
                    portEXIT_CRITICAL(&s_lock);
                }
                ESP_LOGI(TAG, "rec %lu end: %.1f s, %lu chunks%s", (unsigned long)s_cur.id,
                         s_cur_bytes / 32000.0, (unsigned long)s_cur.total_chunks,
                         s_write_error ? " (WRITE ERROR)" : "");
            }
            break;
        }
        ring_release();
    }
}

/* ---------------- 启动：挂卡 + 恢复扫描 ---------------- */

static void set_confirm(const rec_meta_t *m)
{
    /* 已有一段待确认时，多出来的段转 pending 自动上传（正常不会出现多段） */
    portENTER_CRITICAL(&s_lock);
    bool free_slot = !s_await_confirm;
    if (free_slot) {
        s_confirm = *m;
        s_await_confirm = true;
    }
    portEXIT_CRITICAL(&s_lock);
    if (!free_slot) {
        rec_meta_t p = *m;
        p.status = REC_PENDING;
        meta_write(&p);
        s_pending++;
    }
}

static void recover(void)
{
    DIR *d = opendir(REC_DIR);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        char *end;
        uint32_t id = strtoul(e->d_name, &end, 10);
        if (end == e->d_name || strcmp(end, ".json")) continue;

        rec_meta_t m;
        char path[48];
        if (!meta_read(id, &m)) {   /* json 损坏：连 pcm 一起清掉 */
            ESP_LOGW(TAG, "rec %lu meta unreadable, removed", (unsigned long)id);
            delete_rec(id);
            continue;
        }
        pcm_path(path, sizeof(path), id);
        struct stat st;
        if (stat(path, &st) != 0) {   /* 有 json 没 pcm */
            meta_path(path, sizeof(path), id);
            remove(path);
            continue;
        }
        switch (m.status) {
        case REC_PENDING:
            s_pending++;
            ESP_LOGI(TAG, "rec %lu pending, will resume from seq %lu", (unsigned long)id,
                     (unsigned long)m.next_seq);
            break;
        case REC_CONFIRM:
            set_confirm(&m);
            break;
        case REC_RECORDING:   /* 录音中断电：按实际字节数封成待确认，用户决定抢救还是丢弃 */
            m.total_chunks = (st.st_size + m.chunk_bytes - 1) / m.chunk_bytes;
            m.duration_ms = st.st_size / 32;
            if (m.total_chunks == 0) {
                delete_rec(id);
            } else {
                m.status = REC_CONFIRM;
                meta_write(&m);
                set_confirm(&m);
                ESP_LOGW(TAG, "rec %lu interrupted by power loss, %lu chunks recovered",
                         (unsigned long)id, (unsigned long)m.total_chunks);
            }
            break;
        }
    }
    closedir(d);

    /* 有 pcm 没 json 的孤儿（BEGIN 后立刻断电） */
    d = opendir(REC_DIR);
    if (d) {
        char path[48];
        while ((e = readdir(d))) {
            char *end;
            uint32_t id = strtoul(e->d_name, &end, 10);
            if (end == e->d_name || strcmp(end, ".pcm")) continue;
            meta_path(path, sizeof(path), id);
            struct stat st;
            if (stat(path, &st) != 0) {
                pcm_path(path, sizeof(path), id);
                remove(path);
            }
        }
        closedir(d);
    }
}

bool sd_store_init(void)
{
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();   /* 默认 20MHz，原型求稳 */
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 4;
    slot.clk = PIN_SD_CLK;
    slot.cmd = PIN_SD_CMD;
    slot.d0 = PIN_SD_D0;
    slot.d1 = PIN_SD_D1;
    slot.d2 = PIN_SD_D2;
    slot.d3 = PIN_SD_D3;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;

    esp_vfs_fat_sdmmc_mount_config_t mount = {
        .format_if_mount_failed = false,   /* 不主动格式化用户的卡 */
        .max_files = 5,
        .allocation_unit_size = 16 * 1024,
    };
    sdmmc_card_t *card;
    esp_err_t err = esp_vfs_fat_sdmmc_mount("/sdcard", &host, &slot, &mount, &card);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SD mount failed: %s（录音功能需要 TF 卡）", esp_err_to_name(err));
        s_card_ok = false;
        return false;
    }
    s_card_ok = true;
    sdmmc_card_print_info(stdout, card);
    mkdir(REC_DIR, 0775);

    nvs_handle_t nvs;
    if (nvs_open("rec", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_get_u32(nvs, "next_id", &s_next_id);
        nvs_close(nvs);
    }
    recover();
    ESP_LOGI(TAG, "SD ready: %u pending, next_id %lu", (unsigned)s_pending, (unsigned long)s_next_id);

    xTaskCreatePinnedToCore(writer_task, "sd_writer", 8192, NULL, 6, NULL, 0);
    return true;
}
