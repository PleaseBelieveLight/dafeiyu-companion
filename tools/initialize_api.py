"""Import persona, public knowledge and optional schedules through native APIs."""

import json
import msvcrt
import sys
import time
import httpx

from workspace import RUNTIME_ROOT as ROOT, SOURCE_ROOT

lock = (ROOT / "run/api-setup.lock").open("a+b")
if lock.tell() == 0:
    lock.write(b"0")
    lock.flush()
lock.seek(0)
try:
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    print("API setup is already running", flush=True)
    sys.exit(0)
SOURCE = SOURCE_ROOT / "content"
settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
credentials = json.loads((ROOT / "config/local-access.json").read_text(encoding="utf-8-sig"))
client = httpx.Client(base_url="http://127.0.0.1:6185/api/v1", timeout=180, trust_env=False)
report = {}


def api(method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    value = response.json()
    if value.get("status") == "error":
        raise RuntimeError(str(value.get("message", "API error")))
    return value.get("data", value)


def items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ["items", "knowledge_bases", "kbs", "documents", "jobs", "data"]:
            if isinstance(value.get(key), list):
                return value[key]
    return []


for attempt in range(90):
    try:
        token = api(
            "POST",
            "/auth/login",
            json={"username": credentials["username"], "password": credentials["password"]},
        )["token"]
        client.headers["Authorization"] = "Bearer " + token
        break
    except (httpx.HTTPError, KeyError):
        if attempt == 89:
            raise
        time.sleep(3)

personas = items(api("GET", "/personas"))
if not any(p.get("persona_id") == "大肥鱼" for p in personas):
    prompt = (SOURCE / "persona/大肥鱼-人格.md").read_text(encoding="utf-8-sig")
    prompt = prompt.split("\n---\n", 1)[-1].strip()
    api(
        "POST",
        "/personas",
        json={
            "persona_id": "大肥鱼",
            "system_prompt": prompt,
            "tools": None,
            "skills": None,
            "begin_dialogs": [],
        },
    )
report["persona"] = "ready"
print("Persona ready", flush=True)

if not settings.get("deepseek_api_key", "").strip():
    print(
        "Persona imported. Configure the chat API before initializing knowledge bases.", flush=True
    )
    sys.exit(0)

knowledge = items(api("GET", "/knowledge-bases"))
for name, filename in [("大肥鱼手册", "指令手册.md"), ("热梗词典", "知识库-热梗词典示例.md")]:
    kb = next((k for k in knowledge if k.get("kb_name", k.get("name")) == name), None)
    if kb is None:
        kb = api(
            "POST",
            "/knowledge-bases",
            json={
                "name": name,
                "embedding_provider_id": "ollama_embedding",
                "chunk_size": 512,
                "chunk_overlap": 64,
            },
        )
    kb_id = kb.get("kb_id", kb.get("id"))
    if not kb_id:
        raise RuntimeError("Knowledge base identifier missing: " + str(kb))
    documents = items(api("GET", f"/knowledge-bases/{kb_id}/documents"))
    if not documents:
        with (SOURCE / "docs" / filename).open("rb") as document:
            uploaded = api(
                "POST",
                f"/knowledge-bases/{kb_id}/documents",
                files={"file": (filename, document, "text/markdown")},
            )
        report[name] = {"uploaded": True, "result": uploaded}
    else:
        report[name] = {"documents": len(documents)}
    print("Knowledge prepared: " + name, flush=True)

tasks = [
    {
        "name": "大肥鱼早安",
        "cron_expression": "0 7 * * *",
        "note": "现在是早上7点。请按大肥鱼的人格，结合最近聊天自然地给主人发早安并叫他起床，长短随表达需要变化，调用发消息工具发送到本任务会话。",
    },
    {
        "name": "大肥鱼晚间逛B站",
        "cron_expression": "30 20 * * *",
        "note": "结合最近聊天和长期记忆选一个主题，用虚拟手机打开B站搜索，比较前三个结果，挑一条看标题、UP主和简介。通过listen_audio按需听正在播放的声音。最后给主人发送一条简短汇报，只讲实际看到或听到的内容，能获取分享链接就附上；随后记下活动摘要。手机不可用时直接说明，不编造结果，不登录、不评论、不点赞、不投币。",
    },
    {
        "name": "大肥鱼催睡觉",
        "cron_expression": "30 23 * * *",
        "note": "现在是晚上23:30。请按大肥鱼的人格，结合最近聊天自然地提醒主人早点休息，长短随表达需要变化，调用发消息工具发送到本任务会话。",
    },
]
(ROOT / "config/task-templates.json").write_text(
    json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8-sig"
)
if settings["owner_qq"].strip():
    existing = items(api("GET", "/cron/jobs"))
    for task in tasks:
        payload = {
            **task,
            "session": "qq:FriendMessage:" + settings["owner_qq"].strip(),
            "persona_id": "大肥鱼",
            "provider_id": "deepseek_chat",
            "timezone": "Asia/Shanghai",
            "enabled": bool(settings["scheduled_enabled"] and settings["deepseek_api_key"].strip()),
            "run_once": False,
        }
        old = next((j for j in existing if j.get("name") == task["name"]), None)
        if old:
            api("PATCH", "/cron/jobs/" + old.get("job_id", old.get("id")), json=payload)
        else:
            api("POST", "/cron/jobs", json=payload)
    report["schedules"] = "configured"
else:
    report["schedules"] = "waiting_for_owner_qq"
(ROOT / "run/api-setup.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8-sig"
)
print("API setup finished", flush=True)
