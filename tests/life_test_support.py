"""Offline AstrBot boundary doubles; no installed SDK or services are imported."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class Plain:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    def __init__(self, text="", *, scope="test:FriendMessage:1001", sender="1001", event_id="m1"):
        self.unified_msg_origin, self.sender = scope, sender
        self.message_obj = types.SimpleNamespace(message_id=event_id)
        self.messages = [Plain(text)]
        self.group_id, self.platform_name = "", "aiocqhttp"
        self.message_type = "FriendMessage"
        self.send = AsyncMock()
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self.group_id

    def get_message_type(self):
        return self.message_type

    def get_messages(self):
        return self.messages

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


def load_plugin(folder):
    root = Path(__file__).resolve().parents[1]
    api = types.ModuleType("astrbot.api")
    api.logger = Mock()
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context, star.Star = object, Star
    star.StarTools = types.SimpleNamespace(get_data_dir=Mock(return_value=folder))
    star.register = lambda *args: lambda cls: cls
    event_api = types.ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object
    decorator = lambda **kwargs: lambda function: function
    event_api.filter = types.SimpleNamespace(
        on_llm_request=decorator, llm_tool=decorator,
        event_message_type=lambda *args, **kwargs: lambda function: function,
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE=1),
    )
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_life_continuity._chat_test_main",
        root / "plugins/astrbot_plugin_life_continuity/main.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"),
                                 "astrbot.api": api, "astrbot.api.star": star,
                                 "astrbot.api.event": event_api}):
        spec.loader.exec_module(module)
    return module
