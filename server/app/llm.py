"""LLM 增强纪要：把听悟生成的基础纪要 + 全文转写 + 用户上传的会议资料，
合成一份结合文档内容的增强版纪要。OpenAI 兼容接口（千问AI平台 / 百炼 / DeepSeek 均可）。
"""
from __future__ import annotations

import json
import os

import requests

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.7-plus").strip()

MAX_TRANSCRIPT_CHARS = 30000
MAX_DOCS_CHARS = 60000

SYSTEM_PROMPT = """你是一名严谨的会议纪要助手。用户会给你三份材料：
1) 由语音转写生成的基础会议纪要；2) 会议全文转写；3) 会议相关文档（方案、报表、PPT 等）。

请输出一份「增强版会议纪要」（Markdown 中文），要求：
- 以会议讨论为事实主线，用文档补充背景、术语、数据与目标，让没参会的人也能看懂
- 结构：会议概要 / 背景与相关资料要点 / 讨论要点与各方观点 / 决议（与文档条款或目标对齐）/ 待办事项（事项、负责人、时间；没提到就写"未明确"）/ 风险与遗留问题
- 文档里的内容用「据《文档名》」标注来源；会议里的内容不要标注
- 只依据提供的材料，禁止编造数据、人名、结论；材料不足的地方明确写"材料中未提及"
- 打点标记的段落是会议发起人认为重要的内容，如材料中有打点信息请重点关注
- 直接输出 Markdown 正文，不要寒暄"""


def llm_ready() -> bool:
    return bool(LLM_BASE_URL and LLM_API_KEY)


def _fmt_transcript(minutes: dict) -> str:
    lines = []
    for s in minutes.get("transcript", []):
        ms = s.get("start_ms", 0) // 1000
        lines.append(f"[{ms // 60:02d}:{ms % 60:02d}] {s.get('speaker', '?')}：{s.get('text', '')}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n…（转写过长，已截断）"
    return text


def enhance_minutes(minutes: dict, docs: list[tuple[str, str]]) -> str:
    """docs = [(文件名, 提取出的文本)]；返回增强纪要 Markdown。"""
    if not llm_ready():
        raise RuntimeError("未配置 LLM_API_KEY，无法生成增强纪要")

    base = {k: minutes.get(k) for k in
            ("title", "date", "summary", "key_points", "actions", "chapters",
             "speaker_summaries", "next_steps", "marks_ms")}
    doc_parts = []
    total = 0
    for name, text in docs:
        if total >= MAX_DOCS_CHARS:
            doc_parts.append("…（文档过多，其余已略去）")
            break
        remain = MAX_DOCS_CHARS - total
        if len(text) > remain:
            text = text[:remain] + "\n…（已截断）"
        doc_parts.append(f"### 《{name}》\n{text}")
        total += len(text)

    user = (
        "【基础会议纪要】\n" + json.dumps(base, ensure_ascii=False, indent=1)
        + "\n\n【会议全文转写】\n" + _fmt_transcript(minutes)
        + "\n\n【会议相关文档】\n" + "\n\n".join(doc_parts)
        + "\n\n请生成增强版会议纪要。"
    )
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": 16000,   # qwen3.7-plus 含推理过程，留足空间
        },
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 {r.status_code}：{r.text[:300]}")
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return content, usage
