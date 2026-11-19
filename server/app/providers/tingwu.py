"""
通义听悟（经典 OpenAPI 2023-09-30）离线任务：上传 OSS → 创建任务 → 轮询 → 下载各项结果 → 转成统一纪要结构。

文档：
  CreateTask   https://help.aliyun.com/zh/tingwu/api-tingwu-2023-09-30-createtask
  GetTaskInfo  https://help.aliyun.com/zh/tingwu/api-tingwu-2023-09-30-gettaskinfo

统一纪要结构（与具体服务商无关，渲染页面只认它，以后换 FunASR + 任意大模型也输出同样结构）见 app/minutes/render.py。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import oss2
import requests
from aliyunsdkcore.auth.credentials import AccessKeyCredential
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

NEXT_STEPS_PROMPT = (
    "你是会议纪要助手。只依据下面的会议转写内容，整理“下一步规划”："
    "列出会后要推进的事项，每条一行，格式为“- 事项（负责人：X；时间：Y）”，"
    "转写里没提到负责人或时间就写“未明确”，不要编造。没有就输出“- 无”。\n\n{Transcription}"
)
TITLE_PROMPT = "根据下面的会议转写，给这次会议起一个不超过 20 字的标题，只输出标题本身。\n\n{Transcription}"


class TingwuError(RuntimeError):
    pass


class Tingwu:
    def __init__(self) -> None:
        self.ak = os.environ["ALIYUN_ACCESS_KEY_ID"]
        self.sk = os.environ["ALIYUN_ACCESS_KEY_SECRET"]
        self.appkey = os.environ["TINGWU_APPKEY"]
        self.region = os.getenv("TINGWU_REGION", "cn-beijing")
        self.client = AcsClient(region_id=self.region, credential=AccessKeyCredential(self.ak, self.sk))
        self.bucket = oss2.Bucket(oss2.Auth(self.ak, self.sk), os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"])
        self.prefix = os.getenv("OSS_PREFIX", "meeting-recorder/")

    # ---------- OSS ----------
    def upload(self, path: Path) -> tuple[str, str]:
        """上传音频，返回 (object key, 6 小时有效的签名 URL)。"""
        key = f"{self.prefix}{time.strftime('%Y%m%d')}/{uuid.uuid4().hex[:8]}_{path.name}"
        self.bucket.put_object_from_file(key, str(path))
        return key, self.bucket.sign_url("GET", key, 6 * 3600, slash_safe=True)

    def upload_temporary(self, path: Path) -> tuple[str, str]:
        """本地上传专用：音频只放在临时前缀，转写结束后立即删除。"""
        key = f"{self.prefix}tmp/web-upload/{uuid.uuid4().hex}{path.suffix.lower()}"
        self.bucket.put_object_from_file(key, str(path))
        return key, self.bucket.sign_url("GET", key, 6 * 3600, slash_safe=True)

    def delete(self, key: str) -> None:
        self.bucket.delete_object(key)

    # ---------- 听悟 ----------
    def _request(self, method: str, uri: str, body: dict | None = None, query: dict | None = None) -> dict:
        req = CommonRequest()
        req.set_accept_format("json")
        req.set_domain(f"tingwu.{self.region}.aliyuncs.com")
        req.set_version("2023-09-30")
        req.set_protocol_type("https")
        req.set_method(method)
        req.set_uri_pattern(uri)
        req.add_header("Content-Type", "application/json")
        for k, v in (query or {}).items():
            req.add_query_param(k, v)
        if body is not None:
            req.set_content(json.dumps(body).encode("utf-8"))
        resp = json.loads(self.client.do_action_with_exception(req))
        if str(resp.get("Code")) != "0":
            raise TingwuError(f"{resp.get('Code')}: {resp.get('Message')}")
        return resp["Data"]

    def create_task(self, file_url: str, speaker_count: int = 0) -> str:
        body = {
            "AppKey": self.appkey,
            "Input": {"SourceLanguage": "cn", "FileUrl": file_url, "TaskKey": f"mr_{uuid.uuid4().hex[:12]}"},
            "Parameters": {
                "Transcription": {"DiarizationEnabled": True, "Diarization": {"SpeakerCount": speaker_count}},
                "SummarizationEnabled": True,
                "Summarization": {"Types": ["Paragraph", "Conversational", "QuestionsAnswering"]},
                "MeetingAssistanceEnabled": True,
                "MeetingAssistance": {"Types": ["Actions", "KeyInformation"]},
                "AutoChaptersEnabled": True,
                "TextPolishEnabled": False,
                "CustomPromptEnabled": True,
                "CustomPrompt": {"Contents": [
                    {"Name": "next_steps", "Prompt": NEXT_STEPS_PROMPT, "Model": "tingwu-turbo", "TransType": "chat"},
                    {"Name": "title", "Prompt": TITLE_PROMPT, "Model": "tingwu-turbo", "TransType": "default"},
                ]},
            },
        }
        data = self._request("PUT", "/openapi/tingwu/v2/tasks", body, {"type": "offline"})
        return data["TaskId"]

    def wait(self, task_id: str, timeout_s: int = 1800, on_poll=None) -> dict:
        t0 = time.time()
        while True:
            data = self._request("GET", f"/openapi/tingwu/v2/tasks/{task_id}")
            status = data.get("TaskStatus")
            if on_poll:
                on_poll(status, time.time() - t0)
            if status == "COMPLETED":
                return data
            if status in ("FAILED", "INVALID"):
                raise TingwuError(f"任务失败 {data.get('ErrorCode')}: {data.get('ErrorMessage')}")
            if time.time() - t0 > timeout_s:
                raise TingwuError("等待超时")
            time.sleep(10)

    @staticmethod
    def download_results(task_info: dict, outdir: Path | None = None) -> dict:
        """下载 Result JSON；设备会话可落盘，本地上传只在内存中处理。"""
        if outdir is not None:
            outdir.mkdir(parents=True, exist_ok=True)
        results = {}
        for name, url in (task_info.get("Result") or {}).items():
            if not url:
                continue
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            results[name] = r.json()
            if outdir is not None:
                (outdir / f"{name}.json").write_text(json.dumps(results[name], ensure_ascii=False, indent=2))
        return results


# ---------- 听悟结果 → 统一纪要结构 ----------

def _unwrap(obj: dict, key: str):
    """听悟结果文件外层通常还包一层同名 key，例如 {"Summarization": {...}}。"""
    if isinstance(obj, dict) and key in obj and len(obj) <= 3:
        return obj[key]
    return obj


def to_minutes(results: dict, *, duration_s: float | None = None, marks_ms: list[int] | None = None,
               speaker_names: dict[str, str] | None = None) -> dict:
    names = dict(speaker_names or {})

    def spk(sid) -> str:
        sid = str(sid)
        if sid not in names:
            names[sid] = f"发言人{len(names) + 1}"
        return names[sid]

    tr = _unwrap(results.get("Transcription", {}), "Transcription")
    transcript, talk_ms = [], {}
    for p in tr.get("Paragraphs", []) or tr.get("paragraphs", []):
        words = p.get("Words") or p.get("words") or []
        if not words:
            continue
        sid = p.get("SpeakerId", p.get("speakerId", "0"))
        name = spk(sid)
        start, end = words[0].get("Start", words[0].get("start", 0)), words[-1].get("End", words[-1].get("end", 0))
        transcript.append({"speaker": name, "start_ms": start, "text": "".join(w.get("Text", w.get("text", "")) for w in words)})
        talk_ms[name] = talk_ms.get(name, 0) + max(0, end - start)
    if duration_s is None:
        info = tr.get("AudioInfo") or tr.get("audioInfo") or {}
        duration_s = (info.get("Duration") or info.get("duration") or 0) / 1000

    sm = _unwrap(results.get("Summarization", {}), "Summarization")
    summary = sm.get("ParagraphSummary") or sm.get("paragraphSummary") or ""
    speaker_summaries = [
        {"speaker": s.get("SpeakerName") or spk(s.get("SpeakerId")), "summary": s.get("Summary", "")}
        for s in sm.get("ConversationalSummary") or sm.get("conversationalSummary") or []
    ]
    qa = [{"q": q.get("Question", ""), "a": q.get("Answer", "")}
          for q in sm.get("QuestionsAnsweringSummary") or sm.get("questionsAnsweringSummary") or []]

    ma = _unwrap(results.get("MeetingAssistance", {}), "MeetingAssistance")
    keywords = ma.get("Keywords") or ma.get("keywords") or []
    key_points = [s.get("Text", "") for s in ma.get("KeySentences") or ma.get("keySentences") or []]
    actions = [{"text": a.get("Text", a.get("text", "")), "owner": "", "due": ""}
               for a in ma.get("Actions") or ma.get("actions") or []]

    ch = _unwrap(results.get("AutoChapters", {}), "AutoChapters")
    chapters = [{"start_ms": c.get("Start", c.get("start", 0)), "end_ms": c.get("End", c.get("end", 0)),
                 "title": c.get("Headline", c.get("headline", "")), "summary": c.get("Summary", c.get("summary", ""))}
                for c in (ch if isinstance(ch, list) else [])]

    custom = {}
    cp = _unwrap(results.get("CustomPrompt", {}), "CustomPrompt")
    for item in cp if isinstance(cp, list) else [cp]:
        if isinstance(item, dict):
            custom[item.get("Name", item.get("name", ""))] = item.get("Result", item.get("result", ""))

    return {
        "title": (custom.get("title") or "会议纪要").strip().strip("《》\"“”"),
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "duration_s": duration_s,
        "engine": "通义听悟",
        "speakers": [{"name": n, "talk_s": round(talk_ms.get(n, 0) / 1000)} for n in dict.fromkeys(names.values())],
        "keywords": keywords,
        "summary": summary,
        "key_points": key_points,
        "actions": actions,
        "next_steps": custom.get("next_steps", ""),
        "chapters": chapters,
        "speaker_summaries": speaker_summaries,
        "qa": qa,
        "marks_ms": marks_ms or [],
        "transcript": transcript,
    }
