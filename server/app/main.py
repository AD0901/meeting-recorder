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
import subprocess
import sys
import threading
import time
import traceback
import wave
import zlib
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")

from app.minutes.render import render_html  # noqa: E402
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
        (sdir(sid) / "minutes.json").write_text(json.dumps(minutes, ensure_ascii=False, indent=2))
        page = sdir(sid) / "minutes.html"
        page.write_text(render_html(minutes))
        storage.put(f"sessions/{sid}/minutes.html", page)
        storage.put(f"sessions/{sid}/minutes.json", sdir(sid) / "minutes.json")
        update_meta(sid, status="done", title=minutes.get("title"))
        log(f"✅ {sid} 纪要完成：{PUBLIC_URL}/sessions/{sid}")
        notify(sid, minutes.get("title") or "会议纪要已生成")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        update_meta(sid, status="failed", error=f"{type(e).__name__}: {e}")
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

PAGE_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--dim:#6b7280;--line:#e5e7eb;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b2026;--text:#e8eaed;--dim:#9aa3ad;--line:#2b323a;--accent:#60a5fa}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,"PingFang SC",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px}
h1{font-size:22px;margin:0 0 20px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
td,th{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:500;font-size:13px}
a{color:var(--accent);text-decoration:none}
.st{font-size:13px;padding:2px 8px;border-radius:10px;background:var(--line)}
.done{background:#dcfce7;color:#166534}.failed{background:#fee2e2;color:#991b1b}
@media (prefers-color-scheme:dark){.done{background:#14532d;color:#bbf7d0}.failed{background:#7f1d1d;color:#fecaca}}
.card{background:var(--card);border-radius:12px;padding:24px}
"""


def page(title: str, body: str, refresh: int | None = None) -> HTMLResponse:
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta_refresh}
<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body><main>{body}</main></body></html>""")


def fmt_dur(s) -> str:
    if not s:
        return "—"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def index():
    rows = []
    for m in list_sessions():
        sid = m["session_id"]
        st = m.get("status", "")
        rows.append(f"<tr><td><a href='/sessions/{sid}'>{html.escape(m.get('title') or sid)}</a></td>"
                    f"<td>{m.get('created_at', '')}</td><td>{fmt_dur(m.get('duration_s'))}</td>"
                    f"<td>{len(m.get('marks_ms', []))}</td>"
                    f"<td><span class='st {st}'>{STATUS_ZH.get(st, st)}</span></td></tr>")
    body = ("<h1>会议记录</h1><table><tr><th>会议</th><th>时间</th><th>时长</th><th>打点</th><th>状态</th></tr>"
            + ("".join(rows) or "<tr><td colspan=5>还没有录音。在设备上点 REC 开始。</td></tr>") + "</table>")
    busy = any(m.get("status") not in ("done", "failed") for m in list_sessions())
    return page("会议记录", body, refresh=5 if busy else None)


@app.get("/sessions/{sid}", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def session_page(sid: str):
    meta = load_meta(sid)
    mp = sdir(sid) / "minutes.html"
    if mp.exists():
        return HTMLResponse(mp.read_text())
    st = meta.get("status")
    extra = ""
    if st == "uploading":
        extra = f"已收到 {len(received_seqs(sid))} 秒录音"
    elif st == "failed":
        extra = (html.escape(meta.get("error") or "") +
                 f"<form method='post' action='/sessions/{sid}/retry'><button>重新处理</button></form>")
    elif sdir(sid).joinpath("audio.wav").exists():
        extra = f"<a href='/sessions/{sid}/audio.wav'>下载录音</a>"
    body = (f"<p><a href='/'>← 全部会议</a></p><div class='card'><h1>{sid}</h1>"
            f"<p>状态：<span class='st {st}'>{STATUS_ZH.get(st, st)}</span></p><p>{extra}</p>"
            f"<p style='color:var(--dim)'>本页每 5 秒自动刷新，纪要生成后会直接显示。</p></div>")
    return page(sid, body, refresh=None if st == "failed" else 5)


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
