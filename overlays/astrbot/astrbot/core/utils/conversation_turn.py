"""Route current intent, join small QQ bursts and measure the first reply."""

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from contextlib import closing
from contextvars import ContextVar
from itertools import count

from astrbot.core.message.components import Face, Image, Plain, Record, Video

from .context_parts import chat_turn_preference, set_temporary_context


JOIN_SECONDS = 0.18
_PENDING = {}
_LATEST = OrderedDict()
_MEDIA_CANCEL = OrderedDict()
_TURN = ContextVar("dafeiyu_turn", default=None)
_SEQUENCE = count(1)
_METRIC_KEY = secrets.token_bytes(16)
_FRAGMENT = re.compile(r"(?:你)?(?:看这个|给你看|等一下|等等|还有|看这[个张]|给你瞧瞧)[。！!，,\s]*")
_TASK = re.compile(r"^(?:请|麻烦)?(?:你)?(?:帮我|替我|现在就|立刻|马上|取消|停止|别发|先别|不是|我说的是|明天|设置|查|计算|写|打开|发|读|分析|记挂|完成记挂|/)|报错|复现步骤")
_NO_ADVICE = re.compile(r"(?:不用|不需要|别|不要|不想听).{0,5}(?:建议|分析|说教|解决方案)|只(?:是)?想(?:吐槽|抱怨|发泄)")
_SOCIAL = re.compile(r"^(?:你好|嗨|早安|晚安|谢谢|我来了|在吗|无聊|(?:今天|刚才)?.{0,6}(?:好累|累死了|好困|烦死了|好烦)|哈哈+|嘿嘿+|嗯+|好的?|行)[。！？!?,，\s]*$")
_RECALL = re.compile(r"记得|记忆|回忆|以前|过去|上次|上回|上周|昨天|之前|聊过|还记得|约定|记一下|记住")
_DEVICE = re.compile(r"(?:打开|看看|浏览|启动|截图|控制|登录|播放|点击|操作|访问|搜索).{0,15}(?:虚拟手机|手机|设备|屏幕|网页|浏览器|B站|哔哩哔哩)|(?:截屏|截(?:一|张)?图给我看|用(?:虚拟手机|浏览器)|手机上.{0,6}(?:查|找|搜))", re.I)
_PUBLIC = re.compile(r"(?:联网|上网|网上|搜索|查(?:一下|一查|证|找|询|资料|查)|核实|查明)|(?:最新|实时).{0,10}(?:情况|版本|新闻|价格|赛程)")
_KNOWLEDGE = re.compile(r"手册|知识库|帮助文档|(?:开|关)(?:启|闭)?语音|怎么(?:用|配置).{0,9}(?:语音|大肥鱼|功能)|(?:什么梗|梗的出处|梗是什么意思)")
_INSPECT_VIDEO = re.compile(r"(?:看看|总结|看完|分析|解释|听听|识别|转写|读).{0,12}(?:视频|字幕|这段|讲了|说了|哪里)|(?:视频|这段|里面).{0,12}(?:讲|说|拍|意思|问题|步骤)")
_DEVICE_NAMES = {"get_screenshot", "browser_task", "listen_audio", "local_python", "execute_shell",
                 "shell_session", "file_read", "file_write", "file_edit", "grep"}
_DEVICE_SERVERS = {"browser-use", "phone-mcp", "dafeiyu-admin"}
_MEDIA_STOP = re.compile(r"^(?:取消|停止|不用|别|先别|不要)(?:了|(?:再)?(?:发|生成|画|拍|解析|放|点|播).{0,12}|(?:图片|自拍|视频|点歌|解析))?[。！!\s]*$")
_MUSIC = re.compile(
    r"(?:点|放)(?:一|几|两)?(?:首|支)?歌|(?:来|换|推荐|找|搜)(?:一|几|两)?首"
    r"|(?:来|放|播|播放).{0,12}音乐|听.{0,12}(?:歌|音乐)|歌词"
    r"|(?:播放|放一下).{1,40}|(?:想|要)听(?![你您]|听[你您]|一下[你您])[^，。！？!?\s]"
)
_MUSIC_TOOLS = {"play_song_by_name", "query_lyrics_by_name"}
_MUSIC_STATE = "【本轮点歌状态】"


def is_action(text):
    return bool(_TASK.search(text.strip()))


def wants_knowledge(text):
    return bool(_KNOWLEDGE.search(text or ""))


def wants_video(text):
    return bool(_INSPECT_VIDEO.search(text or ""))


def has_recall_topic(text):
    return bool(_RECALL.search(text or ""))


def select_memories(memories, scope):
    valid = []
    for memory in memories or ():
        metadata = memory.metadata or {}
        if (memory.final_score >= 0.35 and not metadata.get("revoked")
                and metadata.get("source_session_id", scope) == scope):
            valid.append(memory)
    return valid[:2]


def advice_declined(text):
    return bool(_NO_ADVICE.search(text or ""))


def media_cancelled(text):
    return bool(_MEDIA_STOP.fullmatch(re.sub(r"^不是[，,\s]*", "", (text or "").strip())))


def wants_music(text):
    return not media_cancelled(text) and bool(_MUSIC.search(text or ""))


def _music_followup(text, contexts):
    """A bare song title can answer the immediately preceding song question."""
    if not text or len(text) > 80 or is_action(text) or media_cancelled(text):
        return False
    tail = [entry for entry in contexts[-5:] if isinstance(entry, dict)
            and entry.get("role") in {"user", "assistant"}]
    if len(tail) < 2 or tail[-2].get("role") != "user" or tail[-1].get("role") != "assistant":
        return False
    from .chat_efficiency import message_text

    return bool(wants_music(message_text(tail[-2])) and re.search(
        r"歌名|哪(?:一)?首|什么歌|想听什么|哪(?:一)?位歌手|谁的歌", message_text(tail[-1])))


def route_capabilities(event, request):
    tools = request.func_tool
    if event.get_extra("cron_job") or event.get_extra("background_task_result"):
        return
    set_temporary_context(request, _MUSIC_STATE, "")
    if not tools:
        return
    text = event.get_message_str() or ""
    plain = re.sub(r"^(?:\[[^\]\n]{1,70}\]\s*)+", "", text).strip()
    contexts = request.contexts if isinstance(request.contexts, list) else []
    if re.fullmatch(r"(?:好[的啊呀]?|行|可以|继续|接着来|现在就做)[。！!\s]*", plain):
        from .chat_efficiency import message_text

        tail = [entry for entry in contexts[-5:] if isinstance(entry, dict) and entry.get("role") in {"user", "assistant"}]
        if (len(tail) >= 2 and tail[-2].get("role") == "user" and tail[-1].get("role") == "assistant"
                and re.search(r"确认|要我|现在.*(?:吗|么|？)|可以.*(?:吗|么|？)", message_text(tail[-1]))
                and is_action(message_text(tail[-2]))):
            plain = message_text(tail[-2])
    explicit_device = bool(_DEVICE.search(plain) or re.search(r"(?:帮我|请|运行|执行).{0,12}(?:终端|脚本|命令|本机|本地文件)", plain))
    explicit_search = bool(_PUBLIC.search(plain))
    recall = has_recall_topic(plain)
    explicit_meme = bool(re.search(r"(?:发|找|来|存|收藏|收下).{0,8}(?:表情包|这个表情)|表情包.{0,4}(?:发|给我)|(?:偷|收|收藏).{0,5}(?:这张图|一下)", plain))
    explicit_schedule = bool(re.search(r"提醒|日程|定时|催我|到点叫我", plain))
    music = wants_music(plain) or _music_followup(plain, contexts)
    social = bool(_SOCIAL.fullmatch(plain) or chat_turn_preference(plain) in {"company", "guess"} or advice_declined(plain))
    if not plain and any(isinstance(part, (Face, Image)) for part in event.get_messages()):
        social = True
    selected = copy.copy(tools)
    selected.tools = [tool for tool in tools.tools if (
        (explicit_device or (tool.name not in _DEVICE_NAMES and not tool.name.startswith(("browser_", "cua_"))
                             and getattr(tool, "mcp_server_name", None) not in _DEVICE_SERVERS))
        and (explicit_search or not tool.name.startswith("web_search_"))
        and (recall or not (tool.name.startswith(("recall_", "memorize_", "forget_")) or tool.name == "read_memory_evidence"))
        and (wants_knowledge(plain) or tool.name != "astr_kb_search")
        and (explicit_schedule or tool.name != "future_task")
        and (explicit_device or tool.name != "send_message_to_user")
        and (explicit_meme or tool.name not in {"search_meme", "send_meme", "steal_meme"})
        and (music or tool.name not in _MUSIC_TOOLS)
        and (re.search(r"(?:解析|提取|下载).{0,16}(?:https?://|链接|视频)", plain) or tool.name != "parse_media_url")
        and (re.search(r"(?:戳一戳|戳我|戳一下)", plain) or tool.name != "llm_poke_user")
        and (not social or recall and tool.name.startswith(("recall_", "memorize_")))
    )]
    request.func_tool = selected
    music_tools = [tool.name for tool in selected.tools
                   if tool.name in _MUSIC_TOOLS and getattr(tool, "active", True)]
    if music_tools:
        set_temporary_context(request, _MUSIC_STATE,
            "本轮可用点歌工具：" + "、".join(music_tools) + "。"
            "点歌通过工具搜索并发送歌曲，与本人唱歌、普通聊天语音开关不同。"
            "对方明确了歌名或歌手时按其要求调用；只说点首歌且没有指定时，"
            "先自然询问想听哪首或什么类型，不编造歌名，不声称无法点歌。"
            "是否发送成功以工具结果为准，不提前声称已播放。")
    if explicit_meme:
        note_result("meme_requested" if any(t.name in {"search_meme", "send_meme", "steal_meme"}
                                            for t in selected.tools) else "meme_unavailable")


def _mergeable(event):
    components = event.get_messages()
    if not components or any(not isinstance(part, (Plain, Image, Face)) for part in components):
        return False
    return not is_action(event.get_message_str() or "")


async def begin_turn(event):
    """Enter after wake and allowlist checks. Only fragments and bare images wait."""
    if event.get_platform_name() != "aiocqhttp":
        return True
    scope = event.unified_msg_origin
    stamp = next(_SEQUENCE)
    event.set_extra("_fy_turn_sequence", stamp)
    event.set_extra("_fy_protected_action", is_action(event.get_message_str() or ""))
    _LATEST[scope] = stamp
    _LATEST.move_to_end(scope)
    if len(_LATEST) > 2048:
        _LATEST.popitem(last=False)
    text = (event.get_message_str() or "").strip()
    if media_cancelled(text):
        _MEDIA_CANCEL[scope] = stamp
        _MEDIA_CANCEL.move_to_end(scope)
        if len(_MEDIA_CANCEL) > 2048:
            _MEDIA_CANCEL.popitem(last=False)
    previous = _PENDING.pop(scope, None)
    if previous:
        previous_event, merged = previous
        if (_mergeable(event) and _mergeable(previous_event)
                and any(isinstance(part, Image) for item in (event, previous_event) for part in item.get_messages())):
            earlier = previous_event.get_message_str() or ""
            event.message_obj.message = [*previous_event.get_messages(), *event.get_messages()]
            event.message_str = event.message_obj.message_str = "\n".join(filter(None, (earlier, text)))
            if not merged.done():
                merged.set_result(True)
            return True
        if not merged.done():
            merged.set_result(False)
    if not _mergeable(event) or not (_FRAGMENT.fullmatch(text) or not text and any(
        isinstance(part, Image) for part in event.get_messages()
    )):
        return True
    merged = asyncio.get_running_loop().create_future()
    entry = (event, merged)
    _PENDING[scope] = entry
    try:
        return not await asyncio.wait_for(asyncio.shield(merged), JOIN_SECONDS)
    except TimeoutError:
        return True
    finally:
        if _PENDING.get(scope) is entry:
            _PENDING.pop(scope, None)
        if not merged.done():
            merged.cancel()


def current_turn(event):
    stamp = event.get_extra("_fy_turn_sequence")
    return not stamp or event.get_extra("_fy_protected_action") or _LATEST.get(event.unified_msg_origin) == stamp


def latest_turn(event):
    stamp = event.get_extra("_fy_turn_sequence")
    return not stamp or _LATEST.get(event.unified_msg_origin) == stamp


def can_finish_media(event):
    stamp = event.get_extra("_fy_turn_sequence")
    return not stamp or _MEDIA_CANCEL.get(event.unified_msg_origin, 0) <= stamp


def start_metrics(event):
    data = None
    if event.get_platform_name() == "aiocqhttp":
        data = {"start": time.monotonic(), "llm": 0, "context": set(), "results": set(),
                "first": None, "media": 0.0, "transport": 0.0, "channels": set(), "kind": "",
                "trace_turn_id": secrets.token_hex(12)}
    return _TURN.set(data)


def activate_metrics(event):
    data = _TURN.get()
    if data is not None:
        components = event.get_messages()
        media = next((label for cls, label in ((Video, "video"), (Record, "voice"), (Image, "image"))
                      if any(isinstance(part, cls) for part in components)), "")
        data["kind"] = media or ("task" if is_action(event.get_message_str() or "") else "text")
        trace_event("turn.received", {"kind": data["kind"]})


def trace_context():
    data = _TURN.get()
    return {"turn_id": data.get("trace_turn_id", ""), "kind": data["kind"]} if data else None


def trace_event(name, details=None, *, source="qq", data=None):
    data = _TURN.get() if data is None else data
    if data is None:
        return
    try:
        from .chat_efficiency import store_path, trace_module

        module = trace_module()
        base = store_path().parent
        module.event(base, source, name, turn_id=data.get("trace_turn_id", ""), details=details)
        if name == "turn.finished" and module.settings(base)["enabled"]:
            module.refresh_index(base)
    except Exception:
        pass


def note_tool(name, phase, status=""):
    trace_event("tool." + phase, {"name": str(name)[:128], "status": status}, source="tool")


def note_model(source, payload):
    data = _TURN.get()
    if data is None or source != "chat":
        return
    data["llm"] += 1
    for message in reversed(payload.get("messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        text = "\n".join(part.get("text", "") for part in content if isinstance(part, dict)) if isinstance(content, list) else str(content)
        data["context"].update(label for label, marker in (
            ("image", "<image_chat_turn>"), ("memory", "相关记忆"), ("schedule", "【本轮程序日程】"),
            ("research", "【自主生活本轮资料】"), ("followup", "【本轮相关记挂】"),
            ("role_state", "【本轮角色状态】"),
            ("speech", "【本轮媒体状态】"), ("company", "【本轮陪聊】"),
        ) if marker in text)
        break


def note_result(status):
    data = _TURN.get()
    if data is not None:
        data["results"].add(status)
        trace_event("turn.result", {"status": status})


def note_media(duration):
    data = _TURN.get()
    if data is not None:
        data["media"] += duration


def note_delivery(chain, duration):
    data = _TURN.get()
    if data is None or not chain or not chain.chain:
        return
    channels = {"text" if isinstance(part, Plain) else "voice" if isinstance(part, Record) else "media"
                for part in chain.chain if not isinstance(part, Plain) or part.text.strip()}
    if not channels:
        return
    data["channels"].update(channels)
    data["transport"] += duration
    trace_event("qq.delivered", {"channels": sorted(channels), "elapsed_ms": round(duration * 1000)})
    if data["first"] is None:
        data["first"] = time.monotonic() - data["start"]


def finish_metrics(event, token):
    data = _TURN.get()
    _TURN.reset(token)
    if not data or not data["kind"]:
        return
    trace_event("turn.finished", {"kind": data["kind"], "generation_calls": data["llm"],
                                 "contexts": sorted(data["context"]), "results": sorted(data["results"]),
                                 "channels": sorted(data["channels"])}, data=data)
    from .chat_efficiency import connect

    try:
        with closing(connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
                event TEXT PRIMARY KEY, at REAL, kind TEXT, generation_calls INTEGER,
                contexts TEXT, results TEXT, first_reply REAL, media REAL, transport REAL, channels TEXT)""")
            now = time.time()
            raw = f"{event.unified_msg_origin}:{event.message_obj.message_id}"
            identity = hashlib.blake2b(raw.encode(), key=_METRIC_KEY, digest_size=16).hexdigest()
            db.execute("DELETE FROM chat_turns WHERE at<?", (now - 30 * 86400,))
            db.execute("INSERT OR IGNORE INTO chat_turns VALUES (?,?,?,?,?,?,?,?,?,?)", (
                identity, now, data["kind"], data["llm"], json.dumps(sorted(data["context"])),
                json.dumps(sorted(data["results"])), data["first"], data["media"], data["transport"],
                json.dumps(sorted(data["channels"])),
            ))
    except Exception:
        # Metrics are ancillary to delivery and never repeat a send.
        pass
