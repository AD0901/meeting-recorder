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
import json
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

INTERACTION_CSS = """
[hidden]{display:none!important}
.section-fold{border-bottom:0;padding:0}.section-fold>summary{display:flex;align-items:center;gap:14px;list-style:none}.section-fold>summary::-webkit-details-marker{display:none}.section-fold>summary h2{flex:1;margin:0}.section-fold>summary::after{content:"展开";flex-shrink:0;padding:5px 11px;border:1px solid var(--line);border-radius:99px;color:var(--accent);font-size:11px;font-weight:650}.section-fold[open]>summary::after{content:"收起"}.fold-count{margin-left:auto;color:var(--dim);font-size:11px;font-weight:400;letter-spacing:0}.fold-content{padding-top:20px}.section-fold[open]>summary{padding-bottom:16px;border-bottom:1px solid var(--line)}
.meeting-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.meeting-title-row h1{flex:1}
.meeting-actions{flex-shrink:0;padding-top:4px}.tool-button{display:inline-flex;align-items:center;justify-content:center;padding:10px 16px;border:1px solid var(--button-bg);border-radius:10px;background:var(--button-bg);color:var(--button-text);cursor:pointer;font-size:13px;font-weight:650;white-space:nowrap}.tool-button.secondary{border-color:var(--line);background:var(--paper);color:var(--ink)}.tool-button:disabled,.quick-questions button:disabled{cursor:not-allowed;opacity:.45}
.assistant-section h2{margin-bottom:8px}.llm-badge{display:inline-flex;align-items:center;margin-left:4px;padding:3px 8px;border-radius:99px;background:color-mix(in srgb,var(--accent) 11%,transparent);color:var(--accent);font-size:10px;font-weight:650;letter-spacing:0;vertical-align:middle}.assistant-intro{margin:0;color:var(--dim);font-size:13px}.privacy-hint{margin:12px 0 0;color:var(--dim);font-size:11px;line-height:1.7}
.chat-log{display:grid;gap:12px;max-height:520px;margin-top:18px;overflow-y:auto}.chat-empty{padding:20px;border:1px dashed var(--line);border-radius:12px;color:var(--dim);font-size:13px;text-align:center}.chat-message{max-width:88%;padding:13px 16px;border-radius:14px;background:var(--bg)}.chat-message.user{justify-self:end;background:color-mix(in srgb,var(--accent) 12%,var(--paper))}.chat-message.assistant{justify-self:start;border:1px solid var(--line);background:color-mix(in srgb,var(--paper) 90%,var(--bg))}.chat-message.error{max-width:100%;background:#fff0ed;color:#9e4038}.message-role{margin-bottom:4px;color:var(--dim);font-size:11px;font-weight:650}.message-text{line-height:1.8;white-space:pre-wrap}
.quick-questions{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.quick-questions button{padding:6px 10px;border:1px solid var(--line);border-radius:99px;background:var(--paper);color:var(--accent);cursor:pointer;font-size:11px}.quick-questions button:hover{border-color:var(--accent)}
.ask-form{margin-top:14px}.ask-input-row{display:flex;align-items:flex-end;gap:10px}.ask-input-row textarea{flex:1;min-height:54px;max-height:180px;resize:vertical}.ask-form textarea,.minutes-editor input,.minutes-editor textarea{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit;line-height:1.65}.ask-form textarea:focus,.minutes-editor input:focus,.minutes-editor textarea:focus{border-color:var(--accent);outline:3px solid color-mix(in srgb,var(--accent) 15%,transparent)}.ask-status{min-height:20px;margin:8px 0 0;color:var(--dim);font-size:12px}.ask-shortcut{color:var(--dim);font-size:11px}
.minutes-editor{margin-bottom:17px}.editor-intro{margin:-8px 0 20px;color:var(--dim);font-size:13px}.edit-group{margin:0 0 13px;padding:0 17px;border:1px solid var(--line);border-radius:13px}.edit-group summary{padding:14px 0}.edit-fields{padding:0 0 17px}.edit-field{display:grid;gap:6px;margin-bottom:14px}.edit-field:last-child{margin-bottom:0}.edit-field>span,.edit-label{color:var(--dim);font-size:12px;font-weight:600}.edit-field textarea{min-height:92px;resize:vertical}.edit-field textarea.tall{min-height:150px}.edit-item{padding:15px 0;border-top:1px solid var(--line)}.edit-item:first-child{border-top:0}.edit-item-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px;color:var(--dim);font-size:12px;font-weight:650}.edit-item textarea{min-height:72px;margin-top:7px;resize:vertical}.edit-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.edit-empty{color:var(--dim);font-size:13px}.editor-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-top:20px}.form-error{margin:12px 0 0;padding:11px 13px;border-radius:10px;background:#fff0ed;color:#9e4038;font-size:12px}.wrap.editing{grid-template-columns:minmax(0,1fr)}.wrap.editing nav{display:none}
@media (max-width:760px){.meeting-title-row{gap:12px}.meeting-actions{padding-top:0}.chat-message{max-width:96%}.ask-input-row{align-items:stretch;flex-direction:column}.ask-input-row .tool-button{width:100%}.edit-grid{grid-template-columns:1fr}.ask-shortcut{display:none}}
@media print{.assistant-section,.minutes-editor,.meeting-actions{display:none!important}.section-fold:not([open])>.fold-content{display:block!important}.section-fold>summary::after{display:none}}
"""

INTERACTION_SCRIPT = r"""
<script>
(() => {
  const sessionId = __SESSION_ID__;
  const editToggle = document.getElementById('edit-toggle');
  const editor = document.getElementById('minutes-editor');
  const minutesView = document.getElementById('minutes-view');
  const editForm = document.getElementById('edit-form');
  const saveButton = document.getElementById('save-minutes');
  const editError = document.getElementById('edit-error');

  function setEditing(active) {
    editor.hidden = !active;
    minutesView.hidden = active;
    document.querySelector('.wrap').classList.toggle('editing', active);
    editToggle.textContent = active ? '取消编辑' : '编辑纪要';
    editToggle.setAttribute('aria-expanded', String(active));
    if (active) editor.scrollIntoView({block: 'start'});
  }

  function cancelEditing() {
    editForm.reset();
    editor.querySelectorAll('.edit-action[data-added="true"]').forEach(item => item.remove());
    editError.hidden = true;
    setEditing(false);
  }

  editToggle.addEventListener('click', () => editor.hidden ? setEditing(true) : cancelEditing());
  document.getElementById('cancel-edit').addEventListener('click', cancelEditing);

  document.getElementById('add-action').addEventListener('click', () => {
    const template = document.getElementById('action-template');
    const item = template.content.firstElementChild.cloneNode(true);
    document.getElementById('edit-actions').appendChild(item);
    item.querySelector('textarea').focus();
  });

  async function responseError(response) {
    try {
      const data = await response.json();
      if (typeof data.detail === 'string') return data.detail;
    } catch (_) {}
    return `请求失败（${response.status}）`;
  }

  function field(item, name) {
    return item.querySelector(`[data-field="${name}"]`).value;
  }

  function collectMinutes() {
    const value = id => document.getElementById(id).value;
    const actions = Array.from(editor.querySelectorAll('.edit-action')).map(item => ({
      text: field(item, 'text').trim(), owner: field(item, 'owner').trim(), due: field(item, 'due').trim(),
    })).filter(item => item.text);
    const chapters = Array.from(editor.querySelectorAll('.edit-chapter')).map(item => ({
      start_ms: Number(item.dataset.startMs) || 0,
      end_ms: Number(item.dataset.endMs) || 0,
      title: field(item, 'title').trim(), summary: field(item, 'summary').trim(),
    })).filter(item => item.title || item.summary);
    const speakerSummaries = Array.from(editor.querySelectorAll('.edit-speaker')).map(item => ({
      speaker: item.dataset.speaker, summary: field(item, 'summary').trim(),
    })).filter(item => item.summary);
    const qa = Array.from(editor.querySelectorAll('.edit-qa')).map(item => ({
      q: field(item, 'q').trim(), a: field(item, 'a').trim(),
    })).filter(item => item.q || item.a);
    const transcript = Array.from(editor.querySelectorAll('.edit-transcript')).map(item => ({
      speaker: item.dataset.speaker, start_ms: Number(item.dataset.startMs) || 0, text: field(item, 'text').trim(),
    })).filter(item => item.text);
    return {
      title: value('edit-title'),
      keywords: value('edit-keywords').split(/[，,、\n]/).map(text => text.trim()).filter(Boolean),
      summary: value('edit-summary').trim(),
      key_points: value('edit-key-points').split('\n').map(text => text.trim()).filter(Boolean),
      actions,
      next_steps: value('edit-next-steps').trim(),
      chapters,
      speaker_summaries: speakerSummaries,
      qa,
      transcript,
    };
  }

  editForm.addEventListener('submit', async event => {
    event.preventDefault();
    saveButton.disabled = true;
    saveButton.textContent = '正在保存…';
    editError.hidden = true;
    try {
      const response = await fetch('/api/sessions/' + encodeURIComponent(sessionId) + '/minutes', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectMinutes()),
      });
      if (!response.ok) throw new Error(await responseError(response));
      saveButton.textContent = '已保存';
      window.location.reload();
    } catch (error) {
      editError.textContent = error.message || '保存失败，请稍后重试';
      editError.hidden = false;
      saveButton.disabled = false;
      saveButton.textContent = '保存修改';
    }
  });

  const askForm = document.getElementById('ask-form');
  const askInput = document.getElementById('ask-input');
  const askButton = document.getElementById('ask-submit');
  const askStatus = document.getElementById('ask-status');
  const chatLog = document.getElementById('chat-log');
  const chatEmpty = document.getElementById('chat-empty');
  const quickButtons = Array.from(document.querySelectorAll('[data-question]'));
  const history = [];
  let askBusy = false;

  function addMessage(role, text) {
    chatEmpty.hidden = true;
    const item = document.createElement('article');
    item.className = `chat-message ${role}`;
    const label = document.createElement('div');
    label.className = 'message-role';
    label.textContent = role === 'user' ? '你' : (role === 'assistant' ? 'AI 纪要助手' : '提示');
    const content = document.createElement('div');
    content.className = 'message-text';
    content.textContent = text;
    item.append(label, content);
    chatLog.appendChild(item);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function setAskBusy(busy) {
    askBusy = busy;
    askButton.disabled = busy;
    askInput.disabled = busy;
    quickButtons.forEach(button => { button.disabled = busy; });
    askButton.textContent = busy ? '大模型思考中…' : '提问';
    askStatus.textContent = busy ? '正在结合当前纪要和全文转写生成回答…' : '';
  }

  askForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (askBusy) return;
    const question = askInput.value.trim();
    if (!question) return;
    const requestHistory = history.slice(-12);
    addMessage('user', question);
    askInput.value = '';
    setAskBusy(true);
    try {
      const response = await fetch('/api/sessions/' + encodeURIComponent(sessionId) + '/ask', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question, history: requestHistory}),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json();
      const answer = data.answer || '大模型未返回答案，请重试。';
      addMessage('assistant', answer);
      history.push({role: 'user', content: question}, {role: 'assistant', content: answer});
      if (history.length > 12) history.splice(0, history.length - 12);
    } catch (error) {
      addMessage('error', error.message || '问答失败，请稍后重试');
      askInput.value = question;
    } finally {
      setAskBusy(false);
      askInput.focus();
    }
  });

  quickButtons.forEach(button => button.addEventListener('click', () => {
    if (askBusy) return;
    askInput.value = button.dataset.question;
    askForm.requestSubmit();
  }));
  askInput.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      askForm.requestSubmit();
    }
  });
})();
</script>
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


def _e(value) -> str:
    return html.escape(str(value or ""))


def _action_editor(action: dict, *, added: bool = False) -> str:
    added_attr = ' data-added="true"' if added else ""
    return f"""<div class="edit-item edit-action"{added_attr}>
<label class="edit-field"><span>事项（清空后保存可删除）</span><textarea data-field="text" rows="2">{_e(action.get('text'))}</textarea></label>
<div class="edit-grid"><label class="edit-field"><span>负责人</span><input data-field="owner" value="{_e(action.get('owner'))}"></label>
<label class="edit-field"><span>截止时间</span><input data-field="due" value="{_e(action.get('due'))}"></label></div></div>"""


def _editor_html(m: dict) -> str:
    actions = "".join(_action_editor(a) for a in m.get("actions", []))
    chapters = "".join(f"""<div class="edit-item edit-chapter" data-start-ms="{int(c.get('start_ms', 0))}" data-end-ms="{int(c.get('end_ms', 0))}">
<div class="edit-item-head"><span>{_t(c.get('start_ms', 0))}</span><span>时间点保留不变</span></div>
<label class="edit-field"><span>章节标题</span><input data-field="title" value="{_e(c.get('title'))}"></label>
<label class="edit-field"><span>章节摘要</span><textarea data-field="summary">{_e(c.get('summary'))}</textarea></label></div>"""
                       for c in m.get("chapters", []))
    speaker_summaries = "".join(f"""<div class="edit-item edit-speaker" data-speaker="{_e(s.get('speaker'))}">
<div class="edit-item-head"><span>{_e(s.get('speaker'))}</span></div>
<textarea data-field="summary" aria-label="{_e(s.get('speaker'))}的发言总结">{_e(s.get('summary'))}</textarea></div>"""
                                for s in m.get("speaker_summaries", []))
    qa = "".join(f"""<div class="edit-item edit-qa"><label class="edit-field"><span>问题</span><textarea data-field="q">{_e(q.get('q'))}</textarea></label>
<label class="edit-field"><span>回答</span><textarea data-field="a">{_e(q.get('a'))}</textarea></label></div>"""
                 for q in m.get("qa", []))
    transcript = "".join(f"""<div class="edit-item edit-transcript" data-speaker="{_e(s.get('speaker'))}" data-start-ms="{int(s.get('start_ms', 0))}">
<div class="edit-item-head"><span>{_e(s.get('speaker'))}</span><span>{_t(s.get('start_ms', 0))}</span></div>
<textarea data-field="text" aria-label="{_e(s.get('speaker'))}在{_t(s.get('start_ms', 0))}的转写">{_e(s.get('text'))}</textarea></div>"""
                         for s in m.get("transcript", []))
    empty = '<p class="edit-empty">暂无内容。</p>'
    return f"""<section id="minutes-editor" class="minutes-editor" hidden aria-labelledby="editor-title">
<h2 id="editor-title">编辑会议纪要</h2><p class="editor-intro">直接校正识别错误或补充信息。保存后，页面和后续 AI 问答都会使用修改后的内容。</p>
<form id="edit-form">
<details class="edit-group" open><summary>标题、摘要与要点</summary><div class="edit-fields">
<label class="edit-field"><span>会议标题</span><input id="edit-title" maxlength="200" value="{_e(m.get('title'))}" required></label>
<label class="edit-field"><span>关键词（用逗号或换行分隔）</span><input id="edit-keywords" value="{_e('、'.join(m.get('keywords', [])))}"></label>
<label class="edit-field"><span>会议摘要（支持 Markdown 列表）</span><textarea id="edit-summary" class="tall">{_e(m.get('summary'))}</textarea></label>
<label class="edit-field"><span>要点（每行一条）</span><textarea id="edit-key-points">{_e(chr(10).join(m.get('key_points', [])))}</textarea></label>
<label class="edit-field"><span>下一步规划（支持 Markdown 列表）</span><textarea id="edit-next-steps">{_e(m.get('next_steps'))}</textarea></label>
</div></details>
<details class="edit-group"><summary>待办事项</summary><div class="edit-fields"><div id="edit-actions">{actions or empty}</div>
<button id="add-action" class="tool-button secondary" type="button">添加待办</button></div></details>
<details class="edit-group"><summary>章节速览</summary><div class="edit-fields">{chapters or empty}</div></details>
<details class="edit-group"><summary>发言总结</summary><div class="edit-fields">{speaker_summaries or empty}</div></details>
<details class="edit-group"><summary>问答回顾</summary><div class="edit-fields">{qa or empty}</div></details>
<details class="edit-group"><summary>会议全文转写</summary><div class="edit-fields">{transcript or empty}</div></details>
<div class="editor-actions"><button id="save-minutes" class="tool-button" type="submit">保存修改</button>
<button id="cancel-edit" class="tool-button secondary" type="button">取消</button></div>
<p id="edit-error" class="form-error" role="alert" hidden></p></form>
<template id="action-template">{_action_editor({}, added=True)}</template></section>"""


def _assistant_html(enabled: bool) -> str:
    disabled = "" if enabled else " disabled"
    unavailable = "" if enabled else '<p class="form-error">服务端还没配置 LLM_API_KEY，配置并重启服务后即可使用大模型问答。</p>'
    return f"""<section id="assistant" class="assistant-section"><h2>AI 纪要问答 <span class="llm-badge">大模型</span></h2>
<p class="assistant-intro">直接询问这场会议的结论、依据和细节，也可以接着上一个回答继续追问。</p>{unavailable}
<div id="chat-log" class="chat-log" aria-live="polite"><div id="chat-empty" class="chat-empty">还没有提问。可以从下面的快捷问题开始。</div></div>
<div class="quick-questions"><button type="button" data-question="这场会议做出了哪些明确决定？"{disabled}>做出了哪些决定？</button>
<button type="button" data-question="请汇总这场会议的待办事项、负责人和时间。"{disabled}>汇总待办事项</button>
<button type="button" data-question="哪些问题在会议结束时仍没有解决？"{disabled}>还有哪些遗留问题？</button></div>
<form id="ask-form" class="ask-form"><div class="ask-input-row"><textarea id="ask-input" rows="2" maxlength="2000" required placeholder="输入关于这场会议的问题…" aria-label="向大模型提问"{disabled}></textarea>
<button id="ask-submit" class="tool-button" type="submit"{disabled}>提问</button></div>
<p id="ask-status" class="ask-status" aria-live="polite"></p><span class="ask-shortcut">Command / Ctrl + Enter 发送</span></form>
<p class="privacy-hint">提问时，当前纪要、全文转写和本次对话会发送给服务端已配置的大模型；AI 回答可能出错，请结合原文核对。</p></section>"""


def render_html(m: dict, *, session_id: str | None = None, enhance_status: str = "",
                assistant_enabled: bool = False) -> str:
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
        qa = "".join(f'<details><summary>{_inline(q["q"])}</summary><p>{_inline(q["a"])}</p></details>'
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
    rendered_sections = []
    fold_counts = {"qa": f'{len(m.get("qa", []))} 条问答', "transcript": f'{len(m.get("transcript", []))} 段发言'}
    for section_id, title, content in sections:
        if section_id in fold_counts:
            rendered_sections.append(
                f'<section id="{section_id}" class="fold-section"><details class="section-fold">'
                f'<summary><h2>{title}<span class="fold-count">{fold_counts[section_id]}</span></h2></summary>'
                f'<div class="fold-content">{content}</div></details></section>')
        else:
            rendered_sections.append(f'<section id="{section_id}"><h2>{title}</h2>{content}</section>')
    body = "".join(rendered_sections)
    enhancement = ""
    if session_id:
        label, description, action, path, cls = {
            "done": ("这份会议已生成资料增强版", "结合会议讨论与上传资料，查看信息更完整的增强纪要。", "查看增强纪要", "enhanced", "done"),
            "processing": ("正在生成资料增强版", "资料已上传，服务器正在解析并生成增强纪要。", "查看生成进度", "enhanced", "processing"),
            "failed": ("增强纪要生成失败", "可以重新上传会议资料，再次生成增强纪要。", "重新上传资料", "enhanced", "failed"),
        }.get(enhance_status, ("用会议资料增强这份纪要", "上传 Word、PPT、Excel、PDF 等资料，补充讨论中的背景、数据和目标。", "上传会议资料", "enhance", ""))
        enhancement = (f'<aside class="enhance-banner {cls}"><div><strong>{label}</strong><p>{description}</p></div>'
                       f'<a href="/sessions/{html.escape(session_id, quote=True)}/{path}">{action}<span aria-hidden="true"> →</span></a></aside>')

    editor, assistant, edit_button, interaction_script = "", "", "", ""
    if session_id:
        editor = _editor_html(m)
        assistant = _assistant_html(assistant_enabled)
        edit_button = ('<div class="meeting-actions"><button id="edit-toggle" class="tool-button secondary" '
                       'type="button" aria-controls="minutes-editor" aria-expanded="false">编辑纪要</button></div>')
        interaction_script = INTERACTION_SCRIPT.replace("__SESSION_ID__", json.dumps(session_id))
        nav = '<a href="#assistant">AI 问答</a>' + nav

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
.enhance-banner{{display:flex;align-items:center;justify-content:space-between;gap:22px;margin-bottom:17px;padding:20px 22px;border:1px solid color-mix(in srgb,var(--accent) 22%,var(--line));border-radius:17px;background:color-mix(in srgb,var(--accent) 7%,var(--paper))}}
.enhance-banner strong{{font-size:14px}}.enhance-banner p{{margin:4px 0 0;color:var(--dim);font-size:12px;line-height:1.65}}.enhance-banner a{{flex-shrink:0;padding:9px 13px;border-radius:10px;background:var(--button-bg);color:var(--button-text);font-size:12px;font-weight:650}}.enhance-banner.processing{{background:color-mix(in srgb,#fff1d6 60%,var(--paper))}}.enhance-banner.failed{{background:color-mix(in srgb,#fde8e5 55%,var(--paper))}}
@media (max-width:760px){{
.topbar-inner{{padding:16px 18px}}.doc-label{{display:none}}.wrap{{display:block;padding:14px}}
nav{{position:static;flex-direction:row;overflow-x:auto;gap:4px;margin-bottom:14px;padding:7px;border-radius:13px;scrollbar-width:thin}}.nav-title{{display:none}}nav a{{flex:0 0 auto;padding:8px 11px}}
.meeting-header,section{{padding:22px 18px;border-radius:17px}}.meeting-header{{padding:25px 21px}}h1{{letter-spacing:-.7px}}h2{{font-size:18px;margin-bottom:16px}}
.meta-row{{display:grid;grid-template-columns:1fr 1fr;gap:9px 14px}}.bar{{grid-template-columns:10px 70px minmax(50px,1fr) 34px;gap:7px}}
.todo thead{{display:none}}.todo,.todo tbody,.todo tr,.todo td{{display:block;width:100%}}.todo tr{{padding:13px 0;border-bottom:1px solid var(--line)}}.todo tr:last-child{{border-bottom:0}}
.todo td{{display:flex;justify-content:space-between;gap:20px;padding:4px 0;border:0;text-align:right}}.todo td::before{{content:attr(data-label);color:var(--dim);font-size:12px;white-space:nowrap}}
.todo td.chk{{display:none}}.todo td:nth-child(2){{display:block;margin-bottom:6px;text-align:left;font-weight:650}}.todo td:nth-child(2)::before{{display:none}}
.timeline li{{grid-template-columns:48px 1fr;gap:11px}}.cards{{grid-template-columns:1fr}}.seg{{padding:12px}}
.enhance-banner{{align-items:flex-start;flex-direction:column;gap:13px;padding:18px}}.enhance-banner a{{width:100%;text-align:center}}
}}
@media print{{
.topbar,nav,.enhance-banner{{display:none}}body{{background:#fff}}.wrap{{display:block;max-width:none;padding:0}}.meeting-header,section{{padding:16px 0;border:0;border-bottom:1px solid #ddd;border-radius:0;box-shadow:none;break-inside:avoid}}
.seg{{break-inside:avoid}}footer{{padding-bottom:0}}
}}
{INTERACTION_CSS}
</style></head><body>
<header class="topbar"><div class="topbar-inner"><a class="brand" href="/" aria-label="返回会议记录"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>adhere</a><span class="doc-label">MEETING NOTES</span></div></header>
<div class="wrap"><nav aria-label="纪要章节"><span class="nav-title">本页目录</span>{nav}</nav><main>
<header class="meeting-header"><div class="eyebrow">AI MEETING NOTES</div>
<div class="meeting-title-row"><h1>{html.escape(m.get("title", "会议纪要"))}</h1>{edit_button}</div>
<div class="meta-row"><span><b>日期</b>{html.escape(m.get("date", ""))}</span><span><b>时长</b>{_dur(m.get("duration_s", 0))}</span>
<span><b>发言人</b>{len(m.get("speakers", []))} 位</span><span><b>重点</b>{len(m.get("marks_ms", []))} 处</span></div>
<div class="bars">{bars}</div></header>
{editor}
<div id="minutes-view">{enhancement}
{assistant}
{body}</div>
<footer>由 {html.escape(m.get("engine", ""))} 生成 · 内容仅依据会议录音整理，请核对后再分发</footer>
</main></div>{interaction_script}</body></html>"""
