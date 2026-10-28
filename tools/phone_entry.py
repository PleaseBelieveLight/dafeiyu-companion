"""Limit PhoneMCP to the dedicated emulator."""

import importlib
import json
import os
import sys

from workspace import RUNTIME_ROOT as ROOT

settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
serial = settings["emulator_serial"]
os.environ["ANDROID_SERIAL"] = serial
sys.path.insert(0, str(ROOT / "phone/src"))
from phone_mcp.adb.adb_binary import get_adb_path
from phone_mcp.adb.connection import ADBConnection


def dedicated_prefix(device_id=None):
    if device_id and device_id != serial:
        raise ValueError("Only the Dafeiyu emulator is allowed")
    return [get_adb_path(), "-s", serial]


for module in ["input", "screenshot", "device", "ocr", "ui_hierarchy"]:
    loaded = importlib.import_module("phone_mcp.adb." + module)
    loaded._get_adb_prefix = dedicated_prefix
original_list = ADBConnection.list_devices


def dedicated_devices(self):
    return [device for device in original_list(self) if device.device_id == serial]


def no_remote_connection(self, *args, **kwargs):
    return False, "Remote device connection is disabled in this installation"


ADBConnection.list_devices = dedicated_devices
ADBConnection.connect = no_remote_connection
ADBConnection.disconnect = no_remote_connection
from phone_mcp.server import run

run(transport="sse", host="127.0.0.1", port=8009, path="/Phone")
