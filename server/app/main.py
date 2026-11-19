"""
会议录音服务端（里程碑 3）：按 PROJECT_BRIEF 第 5 节的协议接收设备分片，
拼成 WAV，然后在后台自动调用转写服务（目前是通义听悟）生成纪要页面。

    cd server
    .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

目录结构（data/ 已在 .gitignore 中）：
    data/sessions/<session_id>/meta.json     会话元数据和状态
    data/sessions/<session_id>/chunks/*.pcm  原始分片（按 seq 命名，重复上传直接覆盖 = 幂等）
    data/sessions/<session_id>/audio.wav     拼好的录音
    data/sessions/<session_id>/minutes.html  纪要页面
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
import zlib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")

from app.docs_extract import MAX_FILE_BYTES, SUPPORTED, ExtractError, extract_text  # noqa: E402
from app.llm import answer_minutes_question, enhance_minutes, llm_ready  # noqa: E402
from app.minutes.render import BASE_CSS, render_html  # noqa: E402
from app.storage import make_storage  # noqa: E402

DATA_DIR = Path(os.getenv("DATA_DIR", SERVER_DIR / "data")) / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)
API_TOKEN = os.getenv("API_TOKEN", "").strip()
ASR_PROVIDER = os.getenv("ASR_PROVIDER", "tingwu").strip().lower()   # tingwu / none
AUTO_OPEN = os.getenv("AUTO_OPEN", "1") == "1"                      # 纪要好了在本机浏览器打开（服务端跑在 Mac 上时）
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
WEB_USER = os.getenv("WEB_USER", "").strip()          # 网页登录（放到公网上时必须设）
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "").strip()

storage = make_storage()
app = FastAPI(title="Meeting Recorder")
_lock = threading.Lock()


# ---------------------------------------------------------------- 工具

def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def sdir(sid: str) -> Path:
    if not sid.replace("-", "").isalnum():
        raise HTTPException(400, "bad session id")
    return DATA_DIR / sid


def load_meta(sid: str) -> dict:
    p = sdir(sid) / "meta.json"
    if not p.exists():
        raise HTTPException(404, "session not found")
    return json.loads(p.read_text())


def save_meta(meta: dict) -> None:
    p = sdir(meta["session_id"]) / "meta.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    tmp.replace(p)


def update_meta(sid: str, **kw) -> dict:
    with _lock:
        meta = load_meta(sid)
        meta.update(kw)
        save_meta(meta)
        return meta


def save_minutes(sid: str, minutes: dict) -> tuple[Path, Path]:
    """原子保存结构化纪要，并同步更新可归档的静态 HTML。"""
    mj = sdir(sid) / "minutes.json"
    tmp = mj.with_suffix(".tmp")
    tmp.write_text(json.dumps(minutes, ensure_ascii=False, indent=2))
    tmp.replace(mj)
    page = sdir(sid) / "minutes.html"
    page.write_text(render_html(minutes))
    return mj, page


def check_token(authorization: str = Header(default="")) -> None:
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(401, "bad token")


def check_web(authorization: str = Header(default="")) -> None:
    """会议列表 / 纪要页面的登录保护（HTTP Basic，浏览器会弹出用户名密码框）。"""
    if not WEB_PASSWORD:
        return
    ok = False
    if authorization.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(authorization[6:]).decode().partition(":")
            ok = hmac.compare_digest(user, WEB_USER) and hmac.compare_digest(pw, WEB_PASSWORD)
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="meeting-recorder"'})


def received_seqs(sid: str) -> list[int]:
    return sorted(int(p.stem) for p in (sdir(sid) / "chunks").glob("*.pcm"))


# ---------------------------------------------------------------- 设备接口（第 5 节）

class SessionIn(BaseModel):
    device_id: str
    started_at: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    format: str = "pcm_s16le"
    device_session: str | None = None   # 设备侧唯一键（启动随机数-会话号），重发 BEGIN 时用来去重


class MarkIn(BaseModel):
    offset_ms: int


class EndIn(BaseModel):
    last_seq: int
    force: bool = False   # 缺片也结束（缺的地方补静音），设备第二次发 END 时用


class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AskIn(BaseModel):
    question: str
    history: list[ChatMessageIn] = []


class ActionEditIn(BaseModel):
    text: str = ""
    owner: str = ""
    due: str = ""


class ChapterEditIn(BaseModel):
    start_ms: int = 0
    end_ms: int = 0
    title: str = ""
    summary: str = ""


class SpeakerSummaryEditIn(BaseModel):
    speaker: str = ""
    summary: str = ""


class QaEditIn(BaseModel):
    q: str = ""
    a: str = ""


class TranscriptEditIn(BaseModel):
    speaker: str = ""
    start_ms: int = 0
    text: str = ""


class MinutesEditIn(BaseModel):
    title: str
    keywords: list[str]
    summary: str
    key_points: list[str]
    actions: list[ActionEditIn]
    next_steps: str
    chapters: list[ChapterEditIn]
    speaker_summaries: list[SpeakerSummaryEditIn]
    qa: list[QaEditIn]
    transcript: list[TranscriptEditIn]


@app.get("/api/health", dependencies=[Depends(check_token)])
def health():
    return {"ok": True, "time": int(time.time())}


@app.post("/api/sessions", dependencies=[Depends(check_token)])
def create_session(body: SessionIn):
    if body.format != "pcm_s16le" or body.channels != 1:
        raise HTTPException(400, "only pcm_s16le mono for now")
    with _lock:
        if body.device_session:  # 设备没收到上次的响应又重发了：返回同一个会话
            for p in DATA_DIR.glob("*/meta.json"):
                m = json.loads(p.read_text())
                if m.get("device_id") == body.device_id and m.get("device_session") == body.device_session:
                    return {"session_id": m["session_id"]}
        sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{body.device_id[-6:]}"
        n = 1
        while (DATA_DIR / sid).exists():
            n += 1
            sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{body.device_id[-6:]}-{n}"
        (DATA_DIR / sid / "chunks").mkdir(parents=True)
        meta = {
            "session_id": sid,
            **body.model_dump(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "uploading",
            "marks_ms": [],
            "last_seq": None,
            "missing_seq": [],
        }
        save_meta(meta)
    log(f"● 会话 {sid} 开始（设备 {body.device_id}）")
    return {"session_id": sid}


@app.post("/api/sessions/{sid}/chunks", dependencies=[Depends(check_token)])
async def upload_chunk(sid: str, seq: int, request: Request, x_crc32: str | None = Header(default=None)):
    d = sdir(sid)
    if not (d / "meta.json").exists():
        raise HTTPException(404, "session not found")
    data = await request.body()
    if len(data) % 2:
        raise HTTPException(400, "odd byte count")
    if x_crc32 and int(x_crc32, 16) != (zlib.crc32(data) & 0xFFFFFFFF):
        raise HTTPException(400, "crc mismatch")  # 设备会重发
    p = d / "chunks" / f"{seq:06d}.pcm"
    tmp = p.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(p)  # 同一个 seq 重复上传直接覆盖：幂等
    if seq % 30 == 0:
        log(f"   {sid} 收到第 {seq} 片")
    return {"ok": True, "seq": seq}


@app.post("/api/sessions/{sid}/marks", dependencies=[Depends(check_token)])
def add_mark(sid: str, body: MarkIn):
    with _lock:
        meta = load_meta(sid)
        if body.offset_ms not in meta["marks_ms"]:  # 重发的打点不重复记
            meta["marks_ms"].append(body.offset_ms)
            meta["marks_ms"].sort()
            save_meta(meta)
    log(f"   ★ {sid} 打点 {body.offset_ms / 1000:.1f} 秒")
    return {"ok": True}


@app.post("/api/sessions/{sid}/end", dependencies=[Depends(check_token)])
def end_session(sid: str, body: EndIn, bg: BackgroundTasks):
    meta = load_meta(sid)
    expected = set(range(body.last_seq + 1)) if body.last_seq >= 0 else set()
    missing = sorted(expected - set(received_seqs(sid)))
    if missing:
        # 设备只在每片都收到 200 之后才发 END，正常不会走到这里；返回缺哪些方便排查
        log(f"⚠️  {sid} END 时缺片 {missing[:10]}{'…' if len(missing) > 10 else ''}")
        if not body.force:
            raise HTTPException(409, {"missing": missing})
    if meta["status"] == "uploading":
        meta = update_meta(sid, status="processing", last_seq=body.last_seq, missing_seq=missing)
        bg.add_task(process_session, sid)
        log(f"■ 会话 {sid} 结束：{body.last_seq + 1} 片齐全，开始处理")
    return {"ok": True, "status": meta["status"], "missing": []}


@app.get("/api/sessions/{sid}", dependencies=[Depends(check_token)])
def session_status(sid: str):
    meta = load_meta(sid)
    meta["received"] = len(received_seqs(sid))
    return meta


@app.get("/api/sessions", dependencies=[Depends(check_web)])
def list_sessions():
    out = []
    for p in sorted(DATA_DIR.glob("*/meta.json"), reverse=True):
        out.append(json.loads(p.read_text()))
    return out


# ---------------------------------------------------------------- 后台处理

def assemble_wav(sid: str, last_seq: int) -> tuple[Path, float]:
    d = sdir(sid)
    meta = load_meta(sid)
    rate = meta.get("sample_rate", 16000)
    wav = d / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for seq in range(last_seq + 1):
            p = d / "chunks" / f"{seq:06d}.pcm"
            w.writeframes(p.read_bytes() if p.exists() else b"\0\0" * rate)
    with wave.open(str(wav)) as r:
        dur = r.getnframes() / r.getframerate()
    return wav, dur


def process_session(sid: str) -> None:
    try:
        meta = load_meta(sid)
        wav, dur = assemble_wav(sid, meta["last_seq"])
        update_meta(sid, status="transcribing" if ASR_PROVIDER != "none" else "done", duration_s=round(dur, 2))
        log(f"   {sid} 已拼成 audio.wav（{dur:.1f} 秒）")
        storage.put(f"sessions/{sid}/audio.wav", wav)

        if ASR_PROVIDER == "none":
            return
        if ASR_PROVIDER != "tingwu":
            raise RuntimeError(f"未知的 ASR_PROVIDER={ASR_PROVIDER}")

        from app.providers.tingwu import Tingwu, to_minutes

        tw = Tingwu()
        _, url = tw.upload(wav)
        task_id = tw.create_task(url)
        update_meta(sid, tingwu_task_id=task_id)
        log(f"   {sid} 听悟任务 {task_id}，等待结果 …")
        info = tw.wait(task_id)
        results = Tingwu.download_results(info, sdir(sid) / "tingwu")
        minutes = to_minutes(results, duration_s=dur, marks_ms=meta.get("marks_ms", []))
        minutes.setdefault("date", meta.get("created_at"))
        mj, page = save_minutes(sid, minutes)
        storage.put(f"sessions/{sid}/minutes.html", page)
        storage.put(f"sessions/{sid}/minutes.json", mj)
        update_meta(sid, status="done", title=minutes.get("title"))
        log(f"✅ {sid} 纪要完成：{PUBLIC_URL}/sessions/{sid}")
        notify(sid, minutes.get("title") or "会议纪要已生成")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        try:
            update_meta(sid, status="failed", error=f"{type(e).__name__}: {e}")
        except Exception:  # 处理中途会话被网页端删除：目录已不在，静默结束
            pass
        log(f"❌ {sid} 处理失败：{e}")


def notify(sid: str, title: str) -> None:
    if sys.platform != "darwin" or not AUTO_OPEN:
        return
    subprocess.run(["osascript", "-e", f'display notification "{title[:60]}" with title "会议纪要已生成"'],
                   check=False)
    subprocess.run(["open", f"{PUBLIC_URL}/sessions/{sid}"], check=False)


# ---------------------------------------------------------------- 网页

STATUS_ZH = {"uploading": "上传中", "processing": "拼接音频", "transcribing": "转写与总结中",
             "done": "已完成", "failed": "失败"}

PAGE_CSS = BASE_CSS + """
body{min-width:320px}
.topbar{max-width:1120px;margin:auto;padding:22px 24px;display:flex;align-items:center;justify-content:space-between}
.brand{display:inline-flex;align-items:center;gap:10px;color:var(--ink);font-size:20px;font-weight:750;letter-spacing:-.5px}
.brand-mark{width:32px;height:32px;border-radius:10px;background:#17221c;display:flex;align-items:center;justify-content:center;gap:3px}
.brand-mark i{display:block;width:3px;border-radius:3px;background:var(--lime)}
.brand-mark i:nth-child(1){height:8px}.brand-mark i:nth-child(2){height:16px}.brand-mark i:nth-child(3){height:11px}
.topbar-note{color:var(--dim);font-size:11px;letter-spacing:1.8px}
.shell{max-width:1080px;margin:0 auto;padding:30px 24px 64px}
.page-heading{display:flex;align-items:end;justify-content:space-between;gap:24px;margin-bottom:28px}
.eyebrow{margin-bottom:9px;color:var(--accent);font-size:11px;font-weight:750;letter-spacing:1.8px}
h1{margin:0;font-size:32px;line-height:1.25;letter-spacing:-1px;overflow-wrap:anywhere}
.page-heading p{margin:10px 0 0;color:var(--dim);font-size:14px}
.count{flex-shrink:0;padding:7px 13px;border:1px solid var(--line);border-radius:99px;color:var(--dim);font-size:12px}
.table-wrap{overflow:hidden;background:var(--paper);border:1px solid var(--line);border-radius:20px;box-shadow:0 12px 30px rgba(23,34,28,.04)}
table{width:100%;border-collapse:collapse}
td,th{padding:17px 18px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
tr:last-child td{border-bottom:0}th{background:color-mix(in srgb,var(--paper) 92%,var(--bg));color:var(--dim);font-size:12px;font-weight:550;white-space:nowrap}
td{color:var(--dim);font-size:13px}td:last-child{width:130px;text-align:right}
.op-cell{display:inline-flex;gap:10px;align-items:center;justify-content:flex-end;flex-wrap:wrap}.secondary-link{display:inline-flex;padding:5px 9px;border-radius:8px;background:color-mix(in srgb,var(--accent) 9%,transparent);color:var(--accent);font-size:12px;white-space:nowrap}.secondary-link.busy{background:#fff1d6;color:#996515}.secondary-link.failed{background:#fde8e5;color:#b8463c}
.doc-body{max-width:860px}.doc-body h1{font-size:26px}.doc-body h2{font-size:19px;margin:26px 0 10px}.doc-body h3{font-size:16px;margin:20px 0 8px}
.doc-body table{display:block;max-width:100%;margin:14px 0;overflow-x:auto}.doc-body td,.doc-body th{padding:9px 11px;border:1px solid var(--line);width:auto;min-width:90px}
.doc-body pre{background:var(--bg);padding:14px;border-radius:10px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.doc-body blockquote{border-left:3px solid var(--accent);margin:14px 0;padding:5px 15px;color:var(--dim);background:var(--bg)}.doc-body img{max-width:100%}.doc-body>*:first-child{margin-top:0}
.enhance-card{max-width:840px}.enhance-intro{margin:0 0 18px;color:var(--dim);font-size:14px;line-height:1.85}
.privacy-note{display:flex;gap:12px;align-items:flex-start;margin:18px 0;padding:14px 16px;border-radius:13px;background:var(--bg);color:var(--dim);font-size:12px;line-height:1.7}.privacy-note strong{flex-shrink:0;color:var(--accent);font-weight:650}
.old-result{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:18px 0;padding:15px 16px;border:1px solid color-mix(in srgb,var(--accent) 25%,var(--line));border-radius:13px;background:color-mix(in srgb,var(--accent) 6%,var(--paper))}.old-result strong{font-size:13px}.old-result p{margin:3px 0 0;color:var(--dim);font-size:12px}
.upload-zone{position:relative;min-height:200px;margin-top:22px;padding:28px;display:grid;place-items:center;border:1.5px dashed color-mix(in srgb,var(--accent) 40%,var(--line));border-radius:18px;background:color-mix(in srgb,var(--accent) 4%,var(--paper));text-align:center;transition:border-color .2s,background .2s}
.upload-zone:hover,.upload-zone.dragging{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--paper))}.upload-zone:focus-within{outline:3px solid color-mix(in srgb,var(--accent) 22%,transparent);outline-offset:3px}
.upload-zone input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer;z-index:2}.upload-prompt{pointer-events:none}.upload-icon{position:relative;width:46px;height:46px;margin:0 auto 14px;border:1px solid color-mix(in srgb,var(--accent) 35%,var(--line));border-radius:14px;background:var(--paper)}.upload-icon::before{content:"+";position:absolute;inset:0;display:grid;place-items:center;color:var(--accent);font-size:27px;font-weight:300}
.upload-title{display:block;color:var(--ink);font-size:16px;font-weight:650}.upload-subtitle{display:block;margin-top:6px;color:var(--dim);font-size:12px}.format-list{display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin-top:15px}.format-list span{padding:3px 8px;border:1px solid var(--line);border-radius:7px;background:var(--paper);color:var(--dim);font-size:10px;font-weight:650}
.form-note{margin:11px 0 0;color:var(--dim);font-size:12px}.file-selection:empty{display:none}.file-summary{margin:15px 0 0;color:var(--dim);font-size:12px}.file-list{display:grid;gap:8px;margin:10px 0 0;padding:0;list-style:none}.file-item{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:11px 13px;border-radius:11px;background:var(--bg);font-size:12px}.file-name{overflow-wrap:anywhere}.file-size{flex-shrink:0;color:var(--dim);white-space:nowrap}.file-item.invalid{background:#fff0ed;color:#9e4038}.file-item.invalid .file-size{color:#9e4038}
.upload-error{display:none;margin-top:12px;padding:11px 13px;border-radius:10px;background:#fff0ed;color:#9e4038;font-size:12px}.upload-error.show{display:block}.form-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-top:20px}.primary-button:disabled{cursor:not-allowed;opacity:.45}
.result-actions{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 20px}.processing-panel{display:flex;align-items:center;gap:17px;padding:20px;border-radius:14px;background:var(--bg)}.processing-panel strong{font-size:14px}.processing-panel p{margin:4px 0 0;color:var(--dim);font-size:12px}.processing-mark{width:28px;height:28px;flex-shrink:0;border:3px solid color-mix(in srgb,var(--accent) 20%,var(--line));border-top-color:var(--accent);border-radius:50%;animation:enhance-spin 1s linear infinite}@keyframes enhance-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.processing-mark{animation:none}.upload-zone{transition:none}}
.meeting-link{display:block;color:var(--ink);overflow-wrap:anywhere}.meeting-link strong{font-size:14px;font-weight:650}.meeting-link span{display:block;margin-top:3px;color:var(--dim);font-size:11px}
.meeting-link:hover strong{color:var(--accent)}
.st{display:inline-flex;align-items:center;padding:4px 10px;border-radius:99px;background:var(--line);color:var(--dim);font-size:12px;white-space:nowrap}
.st.done{background:color-mix(in srgb,var(--accent) 13%,transparent);color:var(--accent)}
.st.failed{background:#fde8e5;color:#b8463c}.st.uploading,.st.processing,.st.transcribing{background:#fff1d6;color:#996515}
.delete-form{margin:0}.danger-link{padding:5px 0;border:0;background:none;color:#b8463c;cursor:pointer;font-size:12px;white-space:nowrap}.danger-link:hover{text-decoration:underline}
.empty{padding:70px 24px;text-align:center}.empty-mark{width:48px;height:48px;margin:0 auto 18px;border:1px solid var(--line);border-radius:15px;display:flex;align-items:center;justify-content:center;gap:4px}
.empty-mark i{width:4px;border-radius:4px;background:var(--accent)}.empty-mark i:nth-child(1){height:12px}.empty-mark i:nth-child(2){height:24px}.empty-mark i:nth-child(3){height:17px}
.empty h2{margin:0 0 8px;color:var(--ink);font-size:18px}.empty p{margin:0;color:var(--dim)}
.status-card{max-width:780px;margin:20px auto 0;padding:36px;background:var(--paper);border:1px solid var(--line);border-radius:22px;box-shadow:0 14px 35px rgba(23,34,28,.04)}
.session-id{margin:8px 0 20px;color:var(--dim);font-size:12px;overflow-wrap:anywhere}.status-line{margin-bottom:28px}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:0;margin:0 0 30px;padding:0;list-style:none}
.step{position:relative;padding-top:35px;color:var(--dim);font-size:13px}.step::before{content:"";position:absolute;top:13px;left:22px;right:0;height:1px;background:var(--line)}.step:last-child::before{display:none}
.step-number{position:absolute;top:0;left:0;width:27px;height:27px;display:grid;place-items:center;border:1px solid var(--line);border-radius:50%;background:var(--paper);font-size:11px;z-index:1}
.step.complete{color:var(--accent)}.step.complete .step-number{border-color:var(--accent);background:var(--accent);color:var(--bg)}
.step.current{color:var(--ink);font-weight:650}.step.current .step-number{border:6px solid color-mix(in srgb,var(--accent) 20%,var(--paper));background:var(--accent);color:transparent}
.progress-note{padding:18px 20px;border-radius:14px;background:var(--bg);color:var(--dim)}.progress-note strong{display:block;color:var(--ink);font-size:23px}.progress-note span{font-size:13px}
.error-box{margin-bottom:18px;padding:15px 17px;border-radius:12px;background:#fff0ed;color:#9e4038;font-size:13px;overflow-wrap:anywhere}
.primary-button,.secondary-button{display:inline-flex;align-items:center;justify-content:center;padding:11px 18px;border-radius:11px;cursor:pointer;font-size:13px;font-weight:650}
.primary-button{border:1px solid var(--button-bg);background:var(--button-bg);color:var(--button-text)}.secondary-button{border:1px solid var(--line);background:var(--paper);color:var(--ink)}
.refresh-note{margin:24px 0 0;color:var(--dim);font-size:12px}
@media (max-width:720px){
.topbar{padding:18px}.topbar-note{display:none}.shell{padding:22px 14px 45px}.page-heading{align-items:flex-start;margin-bottom:22px}h1{font-size:27px}.page-heading p{font-size:13px}.count{margin-top:4px}
.table-wrap{overflow:visible;background:transparent;border:0;box-shadow:none}table,tbody,tr,td{display:block;width:100%}thead{display:none}
tr{margin-bottom:13px;padding:17px 18px;background:var(--paper);border:1px solid var(--line);border-radius:17px}
td{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:6px 0;border:0;text-align:right}td::before{content:attr(data-label);color:var(--dim);font-size:12px;white-space:nowrap}
td:first-child{display:block;padding:0 0 12px;text-align:left}td:first-child::before{display:none}td:last-child{width:100%;margin-top:8px;padding-top:13px;border-top:1px solid var(--line)}
td.empty-cell{display:block;margin:0;padding:0;border:0;text-align:center}td.empty-cell::before{display:none}
.status-card{margin-top:4px;padding:25px 20px}.step{font-size:12px}.step::before{left:20px}.steps{margin-bottom:24px}
.upload-zone{min-height:180px;padding:22px 14px}.old-result{align-items:flex-start;flex-direction:column}.old-result .secondary-button{width:100%;text-align:center}
}
"""


def page(title: str, body: str, refresh: int | None = None) -> HTMLResponse:
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta_refresh}
<title>{html.escape(title)} · Adhere</title><style>{PAGE_CSS}</style></head><body>
<header class="topbar"><a class="brand" href="/" aria-label="Adhere 会议记录首页"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>adhere</a><span class="topbar-note">MEETING NOTES</span></header>
<main class="shell">{body}</main></body></html>""")


def fmt_dur(s) -> str:
    if not s:
        return "—"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def index():
    sessions = list_sessions()
    rows = []
    for m in sessions:
        sid = m["session_id"]
        st = m.get("status", "")
        deletable = st not in ("processing", "transcribing")
        del_btn = (f"<form class='delete-form' method='post' action='/sessions/{sid}/delete' "
                   f"onsubmit=\"return confirm('删除这段录音和纪要？云端不可恢复。设备上已 Save 未传的不受影响。')\">"
                   f"<button class='danger-link' type='submit'>删除</button></form>"
                   if deletable else "<span>—</span>")
        title = html.escape(m.get("title") or sid)
        enhance_link = ""
        if st == "done":
            enhance_st = m.get("enhance_status", "")
            label, path, cls = {
                "done": ("查看增强", "enhanced", ""),
                "processing": ("增强中", "enhanced", "busy"),
                "failed": ("增强失败 · 重试", "enhanced", "failed"),
            }.get(enhance_st, ("资料增强", "enhance", ""))
            enhance_link = (f"<a class='secondary-link {cls}' href='/sessions/{sid}/{path}' "
                            f"title='上传会议资料，AI 结合文档生成增强纪要'>{label}</a>")
        rows.append(f"<tr><td data-label='会议'><a class='meeting-link' href='/sessions/{sid}'><strong>{title}</strong><span>{sid}</span></a></td>"
                    f"<td data-label='时间'>{m.get('created_at', '')}</td><td data-label='时长'>{fmt_dur(m.get('duration_s'))}</td>"
                    f"<td data-label='打点'>{len(m.get('marks_ms', []))} 处</td>"
                    f"<td data-label='状态'><span class='st {st}'>{STATUS_ZH.get(st, st)}</span></td>"
                    f"<td data-label='操作'><span class='op-cell'>{enhance_link}{del_btn}</span></td></tr>")
    empty = ("<tr><td class='empty-cell' colspan='6'><div class='empty'><div class='empty-mark' aria-hidden='true'><i></i><i></i><i></i></div>"
             "<h2>还没有会议记录</h2><p>在设备上点 REC 开始，停止后选 SAVE 上传，会议会自动出现在这里。</p></div></td></tr>")
    body = (f"<div class='page-heading'><div><div class='eyebrow'>MEETING LIBRARY</div><h1>会议记录</h1>"
            f"<p>每一场讨论，都整理成可以回顾与行动的结果。</p></div><span class='count'>{len(sessions)} 场会议</span></div>"
            "<div class='table-wrap'><table><thead><tr><th>会议</th><th>时间</th><th>时长</th><th>打点</th><th>状态</th><th></th></tr></thead><tbody>"
            + ("".join(rows) or empty) + "</tbody></table></div>")
    busy = any(m.get("status") not in ("done", "failed") or m.get("enhance_status") == "processing" for m in sessions)
    return page("会议记录", body, refresh=5 if busy else None)


@app.get("/sessions/{sid}", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def session_page(sid: str):
    meta = load_meta(sid)
    mj = sdir(sid) / "minutes.json"
    mp = sdir(sid) / "minutes.html"
    if mj.exists():
        try:
            return HTMLResponse(render_html(json.loads(mj.read_text()), session_id=sid,
                                            enhance_status=meta.get("enhance_status", ""),
                                            assistant_enabled=llm_ready()))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log(f"⚠️ {sid} 纪要 JSON 无法重渲染，使用原 HTML：{e}")
    if mp.exists():
        return HTMLResponse(mp.read_text())

    st = meta.get("status", "")
    current = {"uploading": 0, "processing": 1, "transcribing": 2}.get(st, -1)
    steps = []
    for i, label in enumerate(("录音上传", "音频拼接", "转写与总结")):
        complete = st == "done" or current > i
        cls = "complete" if complete else ("current" if current == i else "")
        steps.append(f"<li class='step {cls}'><span class='step-number'>{'✓' if complete else i + 1}</span>{label}</li>")

    extra = ""
    if st == "uploading":
        extra = (f"<div class='progress-note'><strong>{len(received_seqs(sid))} 秒</strong>"
                 "<span>云端已接收的录音时长，设备会继续上传。</span></div>")
    elif st == "failed":
        extra = (f"<div class='error-box'>{html.escape(meta.get('error') or '处理过程中出现未知错误')}</div>"
                 f"<form method='post' action='/sessions/{sid}/retry'><button class='primary-button' type='submit'>重新处理</button></form>")
    elif sdir(sid).joinpath("audio.wav").exists():
        extra = f"<a class='secondary-button' href='/sessions/{sid}/audio.wav'>下载录音</a>"

    title = html.escape(meta.get("title") or "会议处理进度")
    note = "可检查错误信息后重新处理。" if st == "failed" else "本页每 5 秒自动刷新，纪要生成后会直接显示。"
    body = (f"<section class='status-card'><div class='eyebrow'>MEETING STATUS</div><h1>{title}</h1>"
            f"<p class='session-id'>{sid}</p><div class='status-line'><span class='st {st}'>{STATUS_ZH.get(st, st)}</span></div>"
            f"<ol class='steps'>{''.join(steps)}</ol>{extra}<p class='refresh-note'>{note}</p></section>")
    return page(meta.get("title") or sid, body, refresh=None if st == "failed" else 5)


@app.post("/api/sessions/{sid}/ask", dependencies=[Depends(check_web)])
def ask_minutes(sid: str, body: AskIn):
    """使用最新保存的纪要和转写调用大模型，支持浏览器内连续追问。"""
    if not llm_ready():
        raise HTTPException(503, "服务端还没配置 LLM_API_KEY，暂时无法使用纪要问答")
    mj = sdir(sid) / "minutes.json"
    if not mj.exists():
        raise HTTPException(400, "会议纪要还没生成")
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "请输入问题")
    if len(question) > 2000:
        raise HTTPException(400, "问题不能超过 2000 字")
    history = body.history[-12:]
    if any(len(item.content) > 4000 for item in history):
        raise HTTPException(400, "对话内容过长，请刷新页面后重新开始")
    minutes = json.loads(mj.read_text())
    try:
        answer, usage = answer_minutes_question(
            minutes, question, [item.model_dump() for item in history])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(502, f"大模型问答失败：{e}") from e
    log(f"💬 {sid} 纪要问答完成，tokens {usage.get('total_tokens', '—')}")
    return {"answer": answer, "usage": usage}


@app.put("/api/sessions/{sid}/minutes", dependencies=[Depends(check_web)])
def edit_minutes(sid: str, body: MinutesEditIn):
    """只更新页面开放编辑的字段，保留说话人统计、时长和打点等原始数据。"""
    if not (sdir(sid) / "minutes.json").exists():
        raise HTTPException(404, "minutes not found")
    with _lock:
        minutes = json.loads((sdir(sid) / "minutes.json").read_text())
        updates = body.model_dump()
        updates["title"] = updates["title"].strip() or "会议纪要"
        minutes.update(updates)
        mj, page = save_minutes(sid, minutes)
    update_meta(sid, title=minutes["title"])
    storage.put(f"sessions/{sid}/minutes.json", mj)
    storage.put(f"sessions/{sid}/minutes.html", page)
    log(f"✏️ {sid} 纪要已人工校正")
    return {"ok": True, "title": minutes["title"]}


@app.get("/sessions/{sid}/audio.wav", dependencies=[Depends(check_web)])
def session_audio(sid: str):
    p = sdir(sid) / "audio.wav"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav", filename=f"{sid}.wav")


@app.post("/sessions/{sid}/retry", dependencies=[Depends(check_web)])
def retry(sid: str, bg: BackgroundTasks):
    meta = load_meta(sid)
    if meta.get("last_seq") is None:
        raise HTTPException(400, "session not ended")
    update_meta(sid, status="processing", error=None)
    bg.add_task(process_session, sid)
    return RedirectResponse(f"/sessions/{sid}", status_code=303)


@app.post("/sessions/{sid}/delete", dependencies=[Depends(check_web)])
def delete_session(sid: str):
    """网页端删除会议（测试录音随手清理）。处理中的会话等完成后再删；
    删除 uploading 会话后，设备补传会收到 404，自动放弃并删掉卡上文件。"""
    meta = load_meta(sid)
    if meta.get("status") in ("processing", "transcribing"):
        raise HTTPException(409, "正在转写/生成纪要，等完成后再删除")
    shutil.rmtree(sdir(sid), ignore_errors=True)
    log(f"🗑 会话 {sid} 已删除")
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- 资料增强纪要（可选功能）

def enhance_task(sid: str, tmp_dir: str) -> None:
    """后台：解析上传的资料 → 调 LLM 生成增强纪要 → 删除上传原文件（不长期存云上）。"""
    tmp = Path(tmp_dir)
    try:
        docs = []
        for p in sorted(tmp.iterdir()):
            try:
                docs.append((p.name, extract_text(p)))
            except ExtractError as e:
                update_meta(sid, enhance_status="failed", enhance_error=f"{p.name}：{e}")
                return
        minutes = json.loads((sdir(sid) / "minutes.json").read_text())
        content, usage = enhance_minutes(minutes, docs)
        (sdir(sid) / "enhanced.md").write_text(content)
        update_meta(sid, enhance_status="done", enhance_usage=usage, enhance_error=None)
        log(f"✨ {sid} 增强纪要完成，tokens {usage.get('total_tokens')}")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        try:
            update_meta(sid, enhance_status="failed", enhance_error=f"{type(e).__name__}: {e}"[:300])
        except Exception:
            pass
        log(f"❌ {sid} 增强纪要失败：{e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # 上传的文档原文件即解析即删，不长期存云上


@app.get("/sessions/{sid}/enhance", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def enhance_form(sid: str):
    meta = load_meta(sid)
    if not (sdir(sid) / "minutes.json").exists():
        raise HTTPException(400, "基础纪要还没生成，等会议处理完成后再来")
    title = html.escape(meta.get("title") or sid)
    if not llm_ready():
        body = (f"<section class='status-card enhance-card'><div class='eyebrow'>DOCUMENT ENHANCED MINUTES</div>"
                f"<h1>资料增强</h1><p class='session-id'>{title}</p>"
                "<div class='error-box'>服务端还没配置 LLM_API_KEY，暂时无法使用。请在 server/.env 中配置后重启服务。</div>"
                f"<a class='secondary-button' href='/sessions/{sid}'>返回原纪要</a></section>")
        return page("资料增强", body)

    old = ""
    if (sdir(sid) / "enhanced.md").exists():
        old = (f"<div class='old-result'><div><strong>已有一份增强纪要</strong><p>重新生成会覆盖现有结果。</p></div>"
               f"<a class='secondary-button' href='/sessions/{sid}/enhanced'>查看增强纪要</a></div>")
    accept = ",".join(sorted(SUPPORTED))
    script = """
<script>
(() => {
  const allowed = new Set(__ALLOWED_EXTENSIONS__);
  const maxBytes = __MAX_FILE_BYTES__;
  const form = document.getElementById('enhance-form');
  const input = document.getElementById('files');
  const zone = document.getElementById('upload-zone');
  const selection = document.getElementById('file-selection');
  const errorBox = document.getElementById('upload-error');
  const submitButton = document.getElementById('enhance-submit');
  let invalidCount = 0;

  const formatSize = bytes => bytes >= 1048576
    ? `${(bytes / 1048576).toFixed(1)} MB`
    : `${Math.max(1, Math.ceil(bytes / 1024))} KB`;

  function renderFiles() {
    const files = Array.from(input.files || []);
    invalidCount = 0;
    selection.replaceChildren();
    errorBox.classList.remove('show');
    errorBox.textContent = '';
    submitButton.disabled = files.length === 0;
    if (!files.length) return;

    const summary = document.createElement('p');
    summary.className = 'file-summary';
    summary.textContent = `已选择 ${files.length} 份资料，共 ${formatSize(files.reduce((sum, file) => sum + file.size, 0))}`;
    selection.appendChild(summary);
    const list = document.createElement('ul');
    list.className = 'file-list';

    for (const file of files) {
      const dot = file.name.lastIndexOf('.');
      const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
      const wrongType = !allowed.has(ext);
      const tooLarge = file.size > maxBytes;
      const reason = wrongType ? '格式不支持' : (tooLarge ? '超过 20MB' : '');
      if (reason) invalidCount += 1;

      const item = document.createElement('li');
      item.className = `file-item${reason ? ' invalid' : ''}`;
      const name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = file.name;
      const size = document.createElement('span');
      size.className = 'file-size';
      size.textContent = `${formatSize(file.size)}${reason ? ` · ${reason}` : ''}`;
      item.append(name, size);
      list.appendChild(item);
    }
    selection.appendChild(list);
    submitButton.disabled = invalidCount > 0;
    if (invalidCount) {
      errorBox.textContent = '请移除不支持的文件或超过 20MB 的文件，然后重新选择。';
      errorBox.classList.add('show');
    }
  }

  input.addEventListener('change', renderFiles);
  for (const eventName of ['dragenter', 'dragover']) {
    zone.addEventListener(eventName, event => {
      event.preventDefault();
      zone.classList.add('dragging');
    });
  }
  zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
  zone.addEventListener('drop', event => {
    event.preventDefault();
    zone.classList.remove('dragging');
    input.files = event.dataTransfer.files;
    renderFiles();
  });
  form.addEventListener('submit', event => {
    if (!input.files.length || invalidCount) {
      event.preventDefault();
      renderFiles();
      return;
    }
    submitButton.disabled = true;
    submitButton.textContent = '正在上传，请稍候…';
  });
  renderFiles();
})();
</script>
"""
    script = script.replace("__ALLOWED_EXTENSIONS__", json.dumps(sorted(SUPPORTED)))
    script = script.replace("__MAX_FILE_BYTES__", str(MAX_FILE_BYTES))
    body = (f"<section class='status-card enhance-card'><div class='eyebrow'>DOCUMENT ENHANCED MINUTES</div>"
            f"<h1>资料增强</h1><p class='session-id'>{title}</p>"
            "<p class='enhance-intro'>上传这场会议的背景材料，AI 会结合基础纪要、全文转写和文档内容，生成信息更完整的增强版会议纪要。</p>"
            "<div class='privacy-note'><strong>原文件如何处理</strong><span>原文件只用于本次解析，处理结束后立即删除，不进入 OSS；云端仅保留最终生成的增强纪要。</span></div>"
            f"{old}<form id='enhance-form' method='post' action='/sessions/{sid}/enhance' enctype='multipart/form-data'>"
            f"<div class='upload-zone' id='upload-zone'><input id='files' type='file' name='files' multiple required accept='{accept}' "
            "aria-label='选择会议资料' aria-describedby='upload-help'>"
            "<div class='upload-prompt' aria-hidden='true'><div class='upload-icon'></div><span class='upload-title'>点击选择或拖拽资料到这里</span>"
            "<span class='upload-subtitle'>可以一次选择多份会议材料</span><div class='format-list'>"
            "<span>WORD</span><span>PPT</span><span>EXCEL</span><span>PDF</span><span>HTML</span><span>MD</span><span>TXT</span>"
            "</div></div></div><p class='form-note' id='upload-help'>每个文件不超过 20MB，支持 Word / PPT / Excel / PDF / HTML / MD / TXT。</p>"
            "<div class='file-selection' id='file-selection' aria-live='polite'></div><div class='upload-error' id='upload-error' role='alert'></div>"
            "<div class='form-actions'><button class='primary-button' id='enhance-submit' type='submit'>上传并生成增强纪要</button>"
            f"<a class='secondary-button' href='/sessions/{sid}'>返回原纪要</a></div></form>"
            "<p class='form-note'>提交后会自动跳转到结果页，处理完成后页面会自动刷新。</p></section>" + script)
    return page("资料增强", body)


@app.post("/sessions/{sid}/enhance", dependencies=[Depends(check_web)])
async def enhance_upload(sid: str, bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    if not llm_ready():
        raise HTTPException(400, "未配置 LLM_API_KEY")
    if not (sdir(sid) / "minutes.json").exists():
        raise HTTPException(400, "基础纪要还没生成")
    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(400, "请选择至少一个文档")
    tmp = DATA_DIR.parent / "tmp" / f"enhance-{sid}-{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for f in files:
            name = Path(f.filename).name   # 去掉路径，防穿越
            if Path(name).suffix.lower() not in SUPPORTED:
                raise HTTPException(400, f"{name}：不支持的格式（Word/PPT/Excel/PDF/HTML/MD/TXT）")
            data = await f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(400, f"{name}：超过 20MB 上限")
            (tmp / name).write_bytes(data)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    update_meta(sid, enhance_status="processing", enhance_error=None)
    bg.add_task(enhance_task, sid, str(tmp))
    log(f"📄 {sid} 收到 {len(files)} 份资料，开始生成增强纪要")
    return RedirectResponse(f"/sessions/{sid}/enhanced", status_code=303)


@app.get("/sessions/{sid}/enhanced", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def enhanced_page(sid: str):
    meta = load_meta(sid)
    st = meta.get("enhance_status", "")
    title = html.escape(meta.get("title") or sid)
    md_file = sdir(sid) / "enhanced.md"
    if st == "processing":
        body = (f"<section class='status-card enhance-card'><div class='eyebrow'>DOCUMENT ENHANCED MINUTES</div>"
                f"<h1>正在生成增强纪要</h1><p class='session-id'>{title}</p>"
                "<div class='status-line'><span class='st transcribing'>处理中</span></div>"
                "<div class='processing-panel'><span class='processing-mark' aria-hidden='true'></span><div>"
                "<strong>正在解析资料并生成内容</strong><p>上传已完成，服务器正在结合会议讨论与文档内容生成增强纪要。</p>"
                "</div></div><p class='refresh-note'>本页每 5 秒自动刷新，完成后会直接显示结果。</p></section>")
        return page("增强纪要生成中", body, refresh=5)
    if st == "failed":
        body = (f"<section class='status-card enhance-card'><div class='eyebrow'>DOCUMENT ENHANCED MINUTES</div>"
                f"<h1>增强纪要生成失败</h1><p class='session-id'>{title}</p>"
                "<div class='status-line'><span class='st failed'>生成失败</span></div>"
                f"<div class='error-box'>{html.escape(meta.get('enhance_error') or '未知错误')}</div>"
                "<div class='form-actions'>"
                f"<a class='primary-button' href='/sessions/{sid}/enhance'>重新上传资料</a>"
                f"<a class='secondary-button' href='/sessions/{sid}'>返回原纪要</a></div></section>")
        return page("增强纪要失败", body)
    if not md_file.exists():
        return RedirectResponse(f"/sessions/{sid}/enhance", status_code=303)
    import markdown
    content = markdown.markdown(md_file.read_text(), extensions=["tables", "fenced_code"])
    body = (f"<div class='page-heading'><div><div class='eyebrow'>ENHANCED MINUTES</div>"
            f"<h1>增强版会议纪要</h1><p class='session-id'>{title} · 由 AI 结合会议资料生成</p></div>"
            "<span class='st done'>已增强</span></div>"
            f"<div class='result-actions'><a class='secondary-button' href='/sessions/{sid}'>返回原纪要</a>"
            f"<a class='primary-button' href='/sessions/{sid}/enhanced.md' download>下载 Markdown</a>"
            f"<a class='secondary-button' href='/sessions/{sid}/enhance'>重新生成</a></div>"
            f"<article class='status-card doc-body'>{content}</article>")
    return page(f"增强纪要 · {meta.get('title') or sid}", body)


@app.get("/sessions/{sid}/enhanced.md", dependencies=[Depends(check_web)])
def enhanced_md(sid: str):
    p = sdir(sid) / "enhanced.md"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="text/markdown; charset=utf-8", filename=f"{sid}-enhanced.md")
