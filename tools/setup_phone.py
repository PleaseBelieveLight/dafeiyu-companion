"""Install the verified input method on the configured dedicated emulator."""

import json
import subprocess

from workspace import RUNTIME_ROOT as ROOT


def main():
    settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))
    serial = settings.get("emulator_serial", "").strip()
    if not serial:
        raise RuntimeError("Configure the dedicated emulator serial first")
    adb = ROOT / "bin/platform-tools/adb.exe"
    apk = ROOT / "downloads/ADBKeyboard.apk"
    if not adb.is_file() or not apk.is_file():
        raise RuntimeError("Run the complete installer before preparing the phone")
    result = subprocess.run(
        [str(adb), "-s", serial, "get-state"], capture_output=True, text=True, check=True
    )
    if result.stdout.strip() != "device":
        raise RuntimeError("The configured emulator is not ready")
    subprocess.run([str(adb), "-s", serial, "install", "-r", str(apk)], check=True)
    subprocess.run(
        [str(adb), "-s", serial, "shell", "ime", "enable", "com.android.adbkeyboard/.AdbIME"],
        check=True,
    )
    print("ADBKeyboard installed on the dedicated emulator: " + serial)


if __name__ == "__main__":
    main()
