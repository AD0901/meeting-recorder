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
import wave
import zlib
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")

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
td{color:var(--dim);font-size:13px}td:last-child{width:64px;text-align:right}
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
        rows.append(f"<tr><td data-label='会议'><a class='meeting-link' href='/sessions/{sid}'><strong>{title}</strong><span>{sid}</span></a></td>"
                    f"<td data-label='时间'>{m.get('created_at', '')}</td><td data-label='时长'>{fmt_dur(m.get('duration_s'))}</td>"
                    f"<td data-label='打点'>{len(m.get('marks_ms', []))} 处</td>"
                    f"<td data-label='状态'><span class='st {st}'>{STATUS_ZH.get(st, st)}</span></td>"
                    f"<td data-label='操作'>{del_btn}</td></tr>")
    empty = ("<tr><td class='empty-cell' colspan='6'><div class='empty'><div class='empty-mark' aria-hidden='true'><i></i><i></i><i></i></div>"
             "<h2>还没有会议记录</h2><p>在设备上点 REC 开始，停止后选 SAVE 上传，会议会自动出现在这里。</p></div></td></tr>")
    body = (f"<div class='page-heading'><div><div class='eyebrow'>MEETING LIBRARY</div><h1>会议记录</h1>"
            f"<p>每一场讨论，都整理成可以回顾与行动的结果。</p></div><span class='count'>{len(sessions)} 场会议</span></div>"
            "<div class='table-wrap'><table><thead><tr><th>会议</th><th>时间</th><th>时长</th><th>打点</th><th>状态</th><th></th></tr></thead><tbody>"
            + ("".join(rows) or empty) + "</tbody></table></div>")
    busy = any(m.get("status") not in ("done", "failed") for m in sessions)
    return page("会议记录", body, refresh=5 if busy else None)


@app.get("/sessions/{sid}", response_class=HTMLResponse, dependencies=[Depends(check_web)])
def session_page(sid: str):
    meta = load_meta(sid)
    mj = sdir(sid) / "minutes.json"
    mp = sdir(sid) / "minutes.html"
    if mj.exists():
        try:
            return HTMLResponse(render_html(json.loads(mj.read_text())))
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
