"""Render private diagnostic records as self-contained, script-free HTML."""

import base64
import hashlib
import html
import json
from datetime import datetime, timedelta, timezone

SOURCE_NAMES = {
    "chat": "普通聊天", "memory": "长期记忆", "context_summary": "上下文摘要",
    "proactive": "主动消息", "life": "自主研究", "meme": "表情包处理",
    "media": "媒体处理", "selfie_planner": "自拍规划", "selfie_caption": "自拍理解",
    "image_caption": "图片转述", "browser": "浏览器助手", "qq": "QQ 会话", "tool": "工具",
}
EVENT_NAMES = {
    "turn.received": "收到消息", "turn.finished": "本轮处理结束", "turn.result": "处理结果",
    "qq.delivered": "QQ 已接收回复", "tool.started": "开始调用工具", "tool.finished": "工具调用结束",
    "request.sent": "已保存实际请求", "response.http": "收到 HTTP 响应",
    "request.finished": "模型调用结束", "capture.failed": "请求记录失败",
    "capture.unavailable": "未取得 HTTP 请求", "capture.finish_failed": "记录收尾失败",
    "capture.response_failed": "响应记录失败",
}
STYLE = """
:root{color-scheme:light;--ink:#182432;--muted:#687582;--line:#e3e9ee;--accent:#087f8c}
*{box-sizing:border-box}body{margin:0;background:#f5f7f9;color:var(--ink);font:15px/1.75
-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif}
main{max-width:1080px;margin:40px auto;padding:0 24px 48px}h1{font-size:28px;line-height:1.35;
margin:8px 0 12px;letter-spacing:-.5px}h2{font-size:17px;margin:0 0 12px}.eyebrow{font-size:12px;
letter-spacing:2px;color:var(--accent);font-weight:700}.sub,small{color:var(--muted)}a{color:var(--accent);
text-decoration:none}a:hover{text-decoration:underline}.bar{display:flex;gap:10px;align-items:center;
flex-wrap:wrap;margin:16px 0 24px}.badge{display:inline-block;border-radius:6px;padding:3px 10px;
background:#e6f3f3;color:#08636e;font-size:12px}.badge.error{background:#fff0ec;color:#af4425}
.card,details{background:white;border:1px solid var(--line);border-radius:12px;padding:20px 24px;
margin:14px 0;overflow:hidden}.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
gap:12px;margin:24px 0}.fact{padding:15px 18px;background:white;border:1px solid var(--line);
border-radius:10px}.fact small{display:block;font-size:12px}.fact b{font-size:17px;word-break:break-word}
summary{cursor:pointer;font-weight:600;outline-offset:5px}details[open]>summary{margin-bottom:18px}
pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.85 'Cascadia Code',Consolas,
'Microsoft YaHei',monospace}code{font-family:Consolas,'Microsoft YaHei',monospace}
.message{border-left:3px solid #d3dee7}.message.user{border-left-color:#0d98a1}.message.assistant{
border-left-color:#8b8ad0}.role{font-size:12px;color:var(--muted);margin-bottom:10px}.media{display:flex;
gap:14px;flex-wrap:wrap;margin-top:14px}.media img{max-width:min(100%,320px);max-height:300px;
object-fit:contain;border-radius:8px;border:1px solid var(--line)}table{width:100%;border-collapse:collapse;
font-size:13px}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}.empty{padding:36px 0;color:var(--muted)}.timeline{font-size:13px}
.timeline p{padding:9px 0;margin:0;border-bottom:1px solid var(--line)}footer{margin-top:28px;
font-size:12px;color:var(--muted)}@media(max-width:700px){main{padding:0 14px;margin-top:24px}
.facts{grid-template-columns:repeat(2,minmax(0,1fr))}.card,details{padding:16px}table{display:block;
overflow:auto}h1{font-size:24px}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def stamp(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")


def page(title, body):
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'img-src \'self\' data: file:; style-src \'unsafe-inline\'; base-uri \'none\'">'
            f'<title>{esc(title)}</title><style>{STYLE}</style><main>{body}</main></html>')


def result_panel(meta):
    status = meta.get("status", "sending")
    labels = {"sending": "请求已记录", "returned": "已返回", "unknown_stream": "流式响应未完整结束",
              "error": "请求异常", "http_error": "接口异常"}
    usage = meta.get("usage") or {}
    elapsed = f'{meta["elapsed"]:.2f} 秒' if meta.get("elapsed") is not None else "等待返回"
    code = meta.get("http_status", "—")
    error = (f'<p class="sub">异常类型：{esc(meta["error_type"])}</p>'
             if meta.get("error_type") else "")
    content = esc(meta.get("output_text") or ("等待模型返回。" if status == "sending"
                  else "本次没有纯文本回复；可查看响应 JSON 或工具调用。"))
    response_link = (f'<p><a href="{esc(meta["response_file"])}" download>下载模型响应 JSON.gz</a></p>'
                     if meta.get("response_file") else "")
    tool_calls = meta.get("tool_calls") or []
    calls = (f'<details><summary>模型请求的工具调用 · {len(tool_calls)} 个</summary>'
             f'<pre>{esc(json.dumps(tool_calls,ensure_ascii=False,indent=2))}</pre></details>' if tool_calls else "")
    return ('<!-- TRACE_RESULT -->'
            f'<div class="facts"><div class="fact"><small>状态</small><b>{esc(labels.get(status,status))}</b></div>'
            f'<div class="fact"><small>耗时 / HTTP</small><b>{elapsed} · {esc(code)}</b></div>'
            f'<div class="fact"><small>输入 tokens</small><b>{esc(usage.get("prompt_tokens", "—"))}</b></div>'
            f'<div class="fact"><small>输出 tokens</small><b>{esc(usage.get("completion_tokens", "—"))}</b></div></div>'
            f'<section class="card"><h2>模型结果</h2>{error}<pre>{content}</pre>{response_link}{calls}</section>'
            '<!-- /TRACE_RESULT -->')


def _image(ref, directory):
    if not isinstance(ref, str):
        return ""
    if not ref.startswith("data:"):
        return f'<p class="sub">图片引用：{esc(ref)}</p>'
    header, _, encoded = ref.partition(",")
    mime = header[5:].split(";", 1)[0].lower()
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
              "image/webp": ".webp", "image/bmp": ".bmp"}.get(mime)
    if not suffix or ";base64" not in header:
        return '<p class="sub">媒体原始数据保留在请求 JSON 中。</p>'
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        return '<p class="sub">图片数据无法预览，原文保留在请求 JSON 中。</p>'
    name = hashlib.sha256(data).hexdigest() + suffix
    media = directory / "media"
    media.mkdir(exist_ok=True)
    path = media / name
    if not path.exists():
        path.write_bytes(data)
    return f'<img loading="lazy" src="media/{name}" alt="请求中的图片">'


def _message(message, directory, number):
    role = str(message.get("role", "unknown"))
    labels = {"system": "系统提示", "user": "用户", "assistant": "助手", "tool": "工具结果",
              "developer": "开发者提示"}
    parts = message.get("content", "")
    if isinstance(parts, str):
        parts = [{"type": "text", "text": parts}]
    texts, media = [], []
    for part in parts or []:
        if not isinstance(part, dict):
            texts.append(str(part))
        elif part.get("type") == "image_url":
            media.append(_image(part.get("image_url", {}).get("url"), directory))
        elif isinstance(part.get("text"), str):
            texts.append(part["text"])
        else:
            texts.append(f'[{part.get("type", "媒体")}：完整数据见原始 JSON]')
    for key in ("tool_calls", "reasoning_content", "tool_call_id", "name"):
        if message.get(key):
            texts.append(key + ": " + json.dumps(message[key], ensure_ascii=False, indent=2))
    text = "\n\n".join(texts)
    return (f'<section class="card message {esc(role)}"><div class="role">'
            f'{number:03d} · {esc(labels.get(role,role))}</div><pre>{esc(text)}</pre>'
            + ('<div class="media">' + "".join(media) + '</div>' if media else "") + '</section>')


def request_page(payload, meta, directory):
    messages = payload.get("messages", [])
    current = next((i for i in range(len(messages) - 1, -1, -1)
                    if messages[i].get("role") == "user"), None)
    systems, history, latest = [], [], []
    for i, message in enumerate(messages):
        rendered = _message(message, directory, i + 1)
        if message.get("role") in {"system", "developer"}:
            systems.append(rendered)
        elif current is not None and i >= current:
            latest.append(rendered)
        else:
            history.append(rendered)
    image_count = sum(1 for m in messages if isinstance(m.get("content"), list)
                      for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
    parameters = {k: v for k, v in payload.items() if k not in {"messages", "tools"}}
    tools = payload.get("tools", [])
    body = ('<div class="eyebrow">DAFEIYU · 调试记录</div><h1>模型请求</h1>'
            f'<div class="sub">{stamp(meta["at"])} · 北京时间 · {esc(meta["id"])}</div>'
            f'<div class="bar"><span class="badge">{esc(SOURCE_NAMES.get(meta["source"],meta["source"]))}</span>'
            f'<span>{esc(meta.get("provider", ""))} / {esc(payload.get("model", ""))}</span>'
            f'<small>{len(messages)} 条消息 · {image_count} 张图片 · {len(tools)} 个工具</small>'
            '<a href="../../index.html">全部记录</a><a href="request.json.gz" download>完整请求 JSON.gz</a></div>'
            + result_panel(meta)
            + '<h2>本轮输入</h2>' + "".join(latest)
            + f'<details><summary>系统提示 · {len(systems)} 条</summary>' + "".join(systems) + '</details>'
            + f'<details><summary>历史上下文 · {len(history)} 条</summary>' + "".join(history) + '</details>'
            + f'<details><summary>工具定义 · {len(tools)} 个</summary><pre>{esc(json.dumps(tools,ensure_ascii=False,indent=2))}</pre></details>'
            + f'<details><summary>请求参数</summary><pre>{esc(json.dumps(parameters,ensure_ascii=False,indent=2))}</pre></details>'
            + '<details><summary>来源与关联 ID</summary>'
            + f'<pre>来源：{esc(meta["source"])}\n调用：{esc(meta["call_id"])}\n轮次：{esc(meta.get("turn_id") or "独立后台调用")}</pre></details>'
            + '<footer>请求取自 HTTP 客户端发送前的实际 JSON 字节。预览保持文字原文，图片单独展示；'
            '完整数据在压缩 JSON 中。鉴权请求头不记录。请求正文、模型结果和图片属于私人运行数据。</footer>')
    return page("大肥鱼 · 模型请求", body)


def index_page(records, enabled, events=()):
    body = ('<div class="eyebrow">DAFEIYU · 调试记录</div><h1>请求与运行时间线</h1>'
            f'<div class="bar"><span class="badge">记录{"已开启" if enabled else "已关闭"}</span>'
            '<small>所有时间均为北京时间 · 按最新记录排序</small></div>')
    rows = []
    for m in records:
        status = {"returned": "已返回", "sending": "已记录", "error": "异常",
                  "http_error": "接口异常", "unknown_stream": "流未完成"}.get(m.get("status"), m.get("status", "—"))
        rows.append(f'<tr><td><a href="requests/{esc(m["id"])}/index.html">{stamp(m["at"])}</a></td>'
                    f'<td>{esc(SOURCE_NAMES.get(m["source"],m["source"]))}</td>'
                    f'<td>{esc(m.get("model", ""))}</td><td>{esc(status)}</td>'
                    f'<td>{m.get("elapsed",0):.2f}s</td></tr>')
    body += ('<section class="card"><h2>模型请求</h2><table><thead><tr><th>时间</th><th>来源</th>'
             '<th>模型</th><th>状态</th><th>耗时</th></tr></thead><tbody>' + "".join(rows)
             + '</tbody></table></section>' if rows else '<div class="empty">尚无已捕获请求。开启后，从下一次实际请求开始记录。</div>')
    if events:
        body += '<section class="card timeline"><h2>近期运行事件</h2>'
        for event in events:
            detail = json.dumps(event.get("details", {}), ensure_ascii=False)
            body += (f'<p><small>{stamp(event["at"])} · {esc(SOURCE_NAMES.get(event["source"],event["source"]))}</small>'
                     f'<br><b>{esc(EVENT_NAMES.get(event["event"],event["event"]))}</b> <span class="sub">{esc(detail)}</span></p>')
        body += '</section>'
    body += '<footer>每个请求可查看系统提示、历史、本轮文字与图片及模型结果。原文不进入公共源码或普通服务日志。</footer>'
    return page("大肥鱼 · 调试记录", body)
