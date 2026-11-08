"""
统一纪要结构 → 单文件 HTML 纪要页（无外部依赖，可直接发给别人打开）。

统一纪要结构（任何转写 / 大模型组合都输出这个 dict）：
{
  "title": str, "date": str, "duration_s": float, "engine": str,
  "speakers": [{"name": str, "talk_s": int}],
  "keywords": [str],
  "summary": str,                     # 全文摘要，可含 markdown 列表
  "key_points": [str],                # 要点 / 关键句
  "actions": [{"text", "owner", "due"}],
  "next_steps": str,                  # markdown 列表
  "chapters": [{"start_ms", "end_ms", "title", "summary"}],
  "speaker_summaries": [{"speaker", "summary"}],
  "qa": [{"q", "a"}],
  "marks_ms": [int],                  # 设备上按 BOOT 打的重点
  "transcript": [{"speaker", "start_ms", "text"}],
}
"""
from __future__ import annotations

import html
import re

PALETTE = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d", "#dc2626"]


def _t(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _dur(sec: float) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h} 小时 {m} 分" if h else (f"{m} 分 {s} 秒" if m else f"{s} 秒")


def _inline(text: str) -> str:
    t = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def _md(text: str) -> str:
    """极简 markdown：标题、无序/有序列表、段落、加粗。"""
    text = (text or "").replace("@#", "\n")
    out, in_list = [], None
    for raw in text.splitlines():
        line = raw.strip()
        m_ul = re.match(r"^[-*•]\s+(.*)", line)
        m_ol = re.match(r"^\d+[.、)]\s*(.*)", line)
        m_h = re.match(r"^#{1,6}\s+(.*)", line)
        kind = "ul" if m_ul else "ol" if m_ol else None
        if in_list and kind != in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if kind:
            if not in_list:
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{_inline((m_ul or m_ol).group(1))}</li>")
        elif m_h:
            out.append(f"<h4>{_inline(m_h.group(1))}</h4>")
        elif line:
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append(f"</{in_list}>")
    return "\n".join(out)


def render_html(m: dict) -> str:
    color = {s["name"]: PALETTE[i % len(PALETTE)] for i, s in enumerate(m.get("speakers", []))}
    total_talk = sum(s.get("talk_s", 0) for s in m.get("speakers", [])) or 1
    sections: list[tuple[str, str, str]] = []  # (id, 标题, html)

    if m.get("summary") or m.get("keywords"):
        kw = "".join(f'<span class="tag">{html.escape(k)}</span>' for k in m.get("keywords", []))
        body = (f'<div class="tags">{kw}</div>' if kw else "") + _md(m.get("summary", ""))
        if m.get("key_points"):
            body += "<h4>要点</h4><ul>" + "".join(f"<li>{_inline(p)}</li>" for p in m["key_points"]) + "</ul>"
        sections.append(("summary", "会议摘要", body))

    if m.get("actions"):
        rows = "".join(
            f'<tr><td class="chk">☐</td><td>{_inline(a["text"])}</td>'
            f'<td>{html.escape(a.get("owner") or "—")}</td><td>{html.escape(a.get("due") or "—")}</td></tr>'
            for a in m["actions"])
        sections.append(("actions", "待办事项",
                         f'<table class="todo"><thead><tr><th></th><th>事项</th><th>负责人</th><th>截止</th></tr>'
                         f"</thead><tbody>{rows}</tbody></table>"))

    if (m.get("next_steps") or "").strip():
        sections.append(("next", "下一步规划", _md(m["next_steps"])))

    if m.get("chapters"):
        items = "".join(
            f'<li><span class="ts">{_t(c["start_ms"])}</span><div><b>{_inline(c["title"])}</b>'
            f'<p>{_inline(c.get("summary", ""))}</p></div></li>' for c in m["chapters"])
        sections.append(("chapters", "章节速览", f'<ol class="timeline">{items}</ol>'))

    if m.get("speaker_summaries"):
        cards = "".join(
            f'<div class="card" style="--c:{color.get(s["speaker"], "#64748b")}">'
            f'<div class="who"><i></i>{html.escape(s["speaker"])}</div><p>{_inline(s["summary"])}</p></div>'
            for s in m["speaker_summaries"])
        sections.append(("speakers", "发言总结", f'<div class="cards">{cards}</div>'))

    if m.get("qa"):
        qa = "".join(f'<details open><summary>{_inline(q["q"])}</summary><p>{_inline(q["a"])}</p></details>'
                     for q in m["qa"])
        sections.append(("qa", "问答回顾", qa))

    if m.get("transcript"):
        marks = sorted(m.get("marks_ms", []))
        lines, mi = [], 0
        for seg in m["transcript"]:
            star = ""
            while mi < len(marks) and marks[mi] <= seg["start_ms"] + 15000 and marks[mi] >= seg["start_ms"] - 15000:
                star = '<span class="mark" title="设备上标记的重点">★ 重点</span>'
                mi += 1
            c = color.get(seg["speaker"], "#64748b")
            lines.append(f'<div class="seg{" hl" if star else ""}"><div class="meta" style="color:{c}">'
                         f'{html.escape(seg["speaker"])}<span class="ts">{_t(seg["start_ms"])}</span>{star}</div>'
                         f'<p>{html.escape(seg["text"])}</p></div>')
        sections.append(("transcript", "会议全文", "".join(lines)))

    bars = "".join(
        f'<div class="bar"><span class="dot" style="background:{color[s["name"]]}"></span>'
        f'<span class="nm">{html.escape(s["name"])}</span>'
        f'<span class="track"><span style="width:{100 * s.get("talk_s", 0) / total_talk:.0f}%;'
        f'background:{color[s["name"]]}"></span></span><span class="pct">{100 * s.get("talk_s", 0) / total_talk:.0f}%</span></div>'
        for s in m.get("speakers", []))
    nav = "".join(f'<a href="#{sid}">{t}</a>' for sid, t, _ in sections)
    body = "".join(f'<section id="{sid}"><h2>{t}</h2>{h}</section>' for sid, t, h in sections)

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(m.get("title", "会议纪要"))}</title><style>
:root{{--bg:#f6f7f9;--paper:#fff;--ink:#1f2328;--dim:#6b7280;--line:#e5e7eb;--accent:#2563eb;--hl:#fff7e6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--paper:#171a21;--ink:#e6e8eb;--dim:#9aa3ad;--line:#2a2f3a;--accent:#60a5fa;--hl:#2b2516}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 16px;display:grid;grid-template-columns:180px 1fr;gap:28px}}
nav{{position:sticky;top:24px;align-self:start;display:flex;flex-direction:column;gap:2px}}
nav a{{color:var(--dim);text-decoration:none;padding:6px 10px;border-radius:6px;font-size:14px}}nav a:hover{{background:var(--paper);color:var(--ink)}}
header,section{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:22px 26px;margin-bottom:16px}}
h1{{font-size:24px;margin:0 0 6px}}h2{{font-size:17px;margin:0 0 14px;padding-left:10px;border-left:3px solid var(--accent)}}h4{{margin:16px 0 6px;font-size:15px}}
.meta-row{{color:var(--dim);font-size:13px;display:flex;flex-wrap:wrap;gap:18px}}
.bars{{margin-top:14px;display:grid;gap:6px}}.bar{{display:grid;grid-template-columns:12px 72px 1fr 40px;align-items:center;gap:8px;font-size:13px}}
.dot{{width:10px;height:10px;border-radius:50%}}.track{{height:6px;background:var(--line);border-radius:3px;overflow:hidden}}.track span{{display:block;height:100%}}.pct{{color:var(--dim);text-align:right}}
.tags{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}}.tag{{background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent);padding:2px 10px;border-radius:999px;font-size:13px}}
p{{margin:6px 0}}ul,ol{{margin:6px 0;padding-left:22px}}
table.todo{{width:100%;border-collapse:collapse;font-size:14px}}.todo th,.todo td{{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}}.todo th{{color:var(--dim);font-weight:500}}.chk{{width:24px;color:var(--dim)}}
.timeline{{list-style:none;padding:0}}.timeline li{{display:grid;grid-template-columns:60px 1fr;gap:12px;padding:8px 0;border-bottom:1px dashed var(--line)}}
.ts{{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;margin-left:8px}}.timeline .ts{{margin:3px 0 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}.card{{border:1px solid var(--line);border-top:3px solid var(--c);border-radius:10px;padding:12px 14px}}
.who{{font-weight:600;display:flex;align-items:center;gap:6px}}.who i{{width:8px;height:8px;border-radius:50%;background:var(--c)}}
details{{border-bottom:1px solid var(--line);padding:8px 0}}summary{{cursor:pointer;font-weight:600}}
.seg{{padding:8px 10px;border-radius:8px}}.seg.hl{{background:var(--hl)}}.seg .meta{{font-size:13px;font-weight:600}}.mark{{margin-left:8px;color:#d97706;font-size:12px}}
footer{{color:var(--dim);font-size:12px;text-align:center;padding:8px}}
@media (max-width:760px){{.wrap{{grid-template-columns:1fr}}nav{{display:none}}header,section{{padding:18px 16px}}}}
@media print{{nav{{display:none}}.wrap{{display:block}}body{{background:#fff}}}}
</style></head><body><div class="wrap"><nav>{nav}</nav><main>
<header><h1>{html.escape(m.get("title", "会议纪要"))}</h1>
<div class="meta-row"><span>📅 {html.escape(m.get("date", ""))}</span><span>⏱ {_dur(m.get("duration_s", 0))}</span>
<span>👥 {len(m.get("speakers", []))} 位发言人</span><span>★ {len(m.get("marks_ms", []))} 处重点</span></div>
<div class="bars">{bars}</div></header>
{body}
<footer>由 {html.escape(m.get("engine", ""))} 生成 · 内容仅依据会议录音整理，请核对后再分发</footer>
</main></div></body></html>"""
