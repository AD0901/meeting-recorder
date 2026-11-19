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

PALETTE = ["#176b47", "#b7791f", "#2563eb", "#b34b75", "#7357b8", "#0f8091", "#6c8f2f", "#c65353"]

BASE_CSS = """
:root{--bg:#f4f5ef;--paper:#fff;--ink:#17221c;--dim:#647168;--line:#dfe3da;--accent:#176b47;--lime:#c9f277;--hl:#edf6df;--button-bg:#17221c;--button-text:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#111712;--paper:#1b241e;--ink:#edf3ee;--dim:#a2aea5;--line:#334138;--accent:#8fd4aa;--hl:#293522;--button-bg:#c9f277;--button-text:#17221c}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 Inter,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:focus-visible,summary:focus-visible,button:focus-visible{outline:3px solid rgba(23,107,71,.3);outline-offset:3px}
button{font:inherit}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


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
            f'<tr><td class="chk">☐</td><td data-label="事项">{_inline(a["text"])}</td>'
            f'<td data-label="负责人">{html.escape(a.get("owner") or "—")}</td>'
            f'<td data-label="截止">{html.escape(a.get("due") or "—")}</td></tr>'
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
<title>{html.escape(m.get("title", "会议纪要"))} · Adhere</title><style>{BASE_CSS}
body{{min-width:320px}}
.topbar{{border-bottom:1px solid var(--line)}}
.topbar-inner{{max-width:1180px;margin:auto;padding:19px 24px;display:flex;align-items:center;justify-content:space-between}}
.brand{{display:inline-flex;align-items:center;gap:10px;color:var(--ink);font-size:20px;font-weight:750;letter-spacing:-.5px}}
.brand-mark{{width:32px;height:32px;border-radius:10px;background:#17221c;display:flex;align-items:center;justify-content:center;gap:3px}}
.brand-mark i{{display:block;width:3px;border-radius:3px;background:var(--lime)}}
.brand-mark i:nth-child(1){{height:8px}}.brand-mark i:nth-child(2){{height:16px}}.brand-mark i:nth-child(3){{height:11px}}
.doc-label{{color:var(--dim);font-size:11px;letter-spacing:1.8px}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px;display:grid;grid-template-columns:180px minmax(0,1fr);gap:24px}}
nav{{position:sticky;top:20px;align-self:start;display:flex;flex-direction:column;gap:3px;padding:15px 12px;background:color-mix(in srgb,var(--paper) 75%,transparent);border:1px solid var(--line);border-radius:16px}}
.nav-title{{padding:4px 10px 9px;color:var(--dim);font-size:10px;font-weight:700;letter-spacing:1.5px}}
nav a{{color:var(--dim);padding:7px 10px;border-radius:8px;font-size:13px;white-space:nowrap}}nav a:hover{{background:var(--bg);color:var(--accent)}}
main{{min-width:0}}
.meeting-header,section{{background:var(--paper);border:1px solid var(--line);border-radius:20px;padding:28px 30px;margin-bottom:17px;box-shadow:0 10px 28px rgba(23,34,28,.035)}}
.meeting-header{{padding:32px}}
.eyebrow{{margin-bottom:11px;color:var(--accent);font-size:11px;font-weight:750;letter-spacing:1.8px}}
h1{{margin:0 0 16px;font-size:clamp(27px,3.5vw,37px);line-height:1.3;letter-spacing:-1.2px;overflow-wrap:anywhere}}
h2{{display:flex;align-items:center;gap:10px;margin:0 0 20px;font-size:19px;letter-spacing:-.3px}}h2::before{{content:"";width:4px;height:18px;border-radius:4px;background:var(--accent)}}
h4{{margin:20px 0 8px;font-size:15px}}p{{margin:8px 0;line-height:1.85}}ul,ol{{margin:8px 0;padding-left:23px}}li{{margin:4px 0}}
section{{scroll-margin-top:20px}}section,section *{{overflow-wrap:anywhere}}
.meta-row{{display:flex;flex-wrap:wrap;gap:10px 22px;color:var(--dim);font-size:12px}}.meta-row b{{margin-right:5px;color:var(--ink);font-weight:550}}
.bars{{margin-top:25px;padding-top:20px;border-top:1px solid var(--line);display:grid;gap:9px}}
.bar{{display:grid;grid-template-columns:12px 84px minmax(60px,1fr) 38px;align-items:center;gap:9px;font-size:12px}}
.dot{{width:9px;height:9px;border-radius:50%}}.nm{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.track{{height:6px;background:var(--line);border-radius:6px;overflow:hidden}}.track span{{display:block;height:100%;border-radius:6px}}.pct{{color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}}
.tags{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:17px}}.tag{{background:color-mix(in srgb,var(--accent) 11%,transparent);color:var(--accent);padding:4px 11px;border-radius:99px;font-size:12px}}
table.todo{{width:100%;border-collapse:collapse;font-size:14px}}.todo th,.todo td{{text-align:left;padding:13px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
.todo th{{color:var(--dim);font-size:12px;font-weight:550}}.todo tr:last-child td{{border-bottom:0}}.chk{{width:28px;color:var(--dim)}}
.timeline{{list-style:none;padding:0}}.timeline li{{position:relative;display:grid;grid-template-columns:58px 1fr;gap:15px;padding:13px 0;border-bottom:1px dashed var(--line)}}
.timeline li:last-child{{border-bottom:0}}.timeline b{{font-size:14px}}.timeline p{{color:var(--dim);font-size:13px}}
.ts{{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums;margin-left:8px}}.timeline .ts{{margin:4px 0 0;color:var(--accent);font-weight:650}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:13px}}
.card{{padding:17px 18px;border:1px solid var(--line);border-top:3px solid var(--c);border-radius:14px;background:color-mix(in srgb,var(--paper) 90%,var(--bg))}}
.who{{display:flex;align-items:center;gap:7px;font-weight:650}}.who i{{width:8px;height:8px;border-radius:50%;background:var(--c)}}.card p{{color:var(--dim);font-size:13px}}
details{{border-bottom:1px solid var(--line);padding:12px 0}}details:last-child{{border-bottom:0}}summary{{cursor:pointer;font-weight:650;line-height:1.6}}details p{{color:var(--dim)}}
.seg{{padding:13px 15px;border-radius:12px}}.seg+.seg{{margin-top:5px}}.seg.hl{{background:var(--hl);box-shadow:inset 3px 0 0 var(--accent)}}
.seg .meta{{display:flex;align-items:center;flex-wrap:wrap;font-size:12px;font-weight:650}}.seg p{{margin:5px 0 0}}.mark{{margin-left:8px;color:#a66a13;font-size:11px}}
footer{{color:var(--dim);font-size:11px;text-align:center;line-height:1.8;padding:15px 10px 30px}}
@media (max-width:760px){{
.topbar-inner{{padding:16px 18px}}.doc-label{{display:none}}.wrap{{display:block;padding:14px}}
nav{{position:static;flex-direction:row;overflow-x:auto;gap:4px;margin-bottom:14px;padding:7px;border-radius:13px;scrollbar-width:thin}}.nav-title{{display:none}}nav a{{flex:0 0 auto;padding:8px 11px}}
.meeting-header,section{{padding:22px 18px;border-radius:17px}}.meeting-header{{padding:25px 21px}}h1{{letter-spacing:-.7px}}h2{{font-size:18px;margin-bottom:16px}}
.meta-row{{display:grid;grid-template-columns:1fr 1fr;gap:9px 14px}}.bar{{grid-template-columns:10px 70px minmax(50px,1fr) 34px;gap:7px}}
.todo thead{{display:none}}.todo,.todo tbody,.todo tr,.todo td{{display:block;width:100%}}.todo tr{{padding:13px 0;border-bottom:1px solid var(--line)}}.todo tr:last-child{{border-bottom:0}}
.todo td{{display:flex;justify-content:space-between;gap:20px;padding:4px 0;border:0;text-align:right}}.todo td::before{{content:attr(data-label);color:var(--dim);font-size:12px;white-space:nowrap}}
.todo td.chk{{display:none}}.todo td:nth-child(2){{display:block;margin-bottom:6px;text-align:left;font-weight:650}}.todo td:nth-child(2)::before{{display:none}}
.timeline li{{grid-template-columns:48px 1fr;gap:11px}}.cards{{grid-template-columns:1fr}}.seg{{padding:12px}}
}}
@media print{{
.topbar,nav{{display:none}}body{{background:#fff}}.wrap{{display:block;max-width:none;padding:0}}.meeting-header,section{{padding:16px 0;border:0;border-bottom:1px solid #ddd;border-radius:0;box-shadow:none;break-inside:avoid}}
.seg{{break-inside:avoid}}footer{{padding-bottom:0}}
}}
</style></head><body>
<header class="topbar"><div class="topbar-inner"><a class="brand" href="/" aria-label="返回会议记录"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>adhere</a><span class="doc-label">MEETING NOTES</span></div></header>
<div class="wrap"><nav aria-label="纪要章节"><span class="nav-title">本页目录</span>{nav}</nav><main>
<header class="meeting-header"><div class="eyebrow">AI MEETING NOTES</div><h1>{html.escape(m.get("title", "会议纪要"))}</h1>
<div class="meta-row"><span><b>日期</b>{html.escape(m.get("date", ""))}</span><span><b>时长</b>{_dur(m.get("duration_s", 0))}</span>
<span><b>发言人</b>{len(m.get("speakers", []))} 位</span><span><b>重点</b>{len(m.get("marks_ms", []))} 处</span></div>
<div class="bars">{bars}</div></header>
{body}
<footer>由 {html.escape(m.get("engine", ""))} 生成 · 内容仅依据会议录音整理，请核对后再分发</footer>
</main></div></body></html>"""
