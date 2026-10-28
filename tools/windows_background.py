"""Start a Windows background process outside the caller's managed process tree."""

import base64
import json
import subprocess
from pathlib import Path


def launch_background(command: list, cwd: Path, env: dict[str, str]) -> int:
    """Use the Windows process provider while retaining this deployment's environment."""
    payload = base64.b64encode(json.dumps({
        "command_line": subprocess.list2cmdline([str(part) for part in command]),
        "cwd": str(cwd),
    }, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$variables = @([Environment]::GetEnvironmentVariables().GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
    ShowWindow = [uint16]0
    EnvironmentVariables = [string[]]$variables
}
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $payload.command_line
    CurrentDirectory = $payload.cwd
    ProcessStartupInformation = $startup
}
if ($result.ReturnValue -ne 0) { throw ('Background launch failed: ' + $result.ReturnValue) }
[Console]::WriteLine($result.ProcessId)
""".replace("__PAYLOAD__", payload)
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True, timeout=30,
    )
    try:
        pid = int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Windows did not return a background process identifier") from exc
    if pid <= 0:
        raise RuntimeError("Windows returned an invalid background process identifier")
    return pid
