"""Download the local embedding model without starting the QQ bot."""

from pathlib import Path

from install import download_model, find_program, read_json
from workspace import RUNTIME_ROOT as ROOT



if __name__ == "__main__":
    settings = read_json(ROOT / "config/settings.json")
    program = (
        Path(settings["ollama_path"])
        if settings.get("ollama_path")
        else find_program("ollama.exe") or ROOT / "bin/ollama/ollama.exe"
    )
    download_model(ROOT, program)
