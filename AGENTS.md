# Repository Guidelines

## Project Structure & Module Organization

This repository packages a Windows 10/11 x64 deployment of the Dafeiyu QQ bot using Python 3.12.

Workspace roles: `../dafeiyu-astrbot-main/` is the latest third-party upstream reference; this repository is the private development project; `../Dafeiyu/` stores the live runtime, dependencies, caches, credentials, and persistent user data. Do not treat that data as disposable. All project tools use `tools/workspace.py` to resolve these paths. Never depend on `Dafeiyu/source/dafeiyu-astrbot-main/`.

- `tools/`: installation, configuration GUI, service control, MCP servers, and ASR utilities.
- `plugins/`: bundled AstrBot plugins; `overlays/`: patches copied into installed AstrBot and PhoneMCP sources. Make persistent changes here.
- `content/`: persona and knowledge documents; `assets/`: shared images and attribution.
- `config/settings.example.json`: public configuration template.
- `tests/test_distribution.py`: source distribution checks; `tests/runtime/`: installed Python and Node.js regressions.
- `dependencies.lock.json` and `requirements/*.lock.txt`: pinned upstream sources and separate Python environments.

## Build, Test, and Development Commands

Run from the repository root in PowerShell:

```powershell
.\安装.cmd                                      # Download dependencies and create environments
.\配置向导.cmd                                  # Configure an installed deployment
.\启动全部.cmd                                  # Start local services
.\停止全部.cmd                                  # Stop local services
.\部署更新.cmd                                  # Deploy patches without reinstalling
py -3.12 -m unittest discover -s tests -v        # Source distribution tests
py -3.12 tools/check_release.py                # Public-file and credential checks
py -3.12 -m compileall -q tools plugins overlays # CI syntax checks
python tools/launch.py verify                  # Installed regressions
```

There is no separate application build; the installer assembles locked dependencies and applies local patches.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow adjacent code and preserve upstream conventions in overlays. Ruff targets Python 3.12 with a 100-character line setting; run `ruff check .` when available. Its exclusions are defined in `pyproject.toml`; CI does not currently run Ruff. Preserve LF for Python/JSON/Markdown and CRLF for `.cmd`/`.ps1`, per `.gitattributes`.

## Testing Guidelines

Use standard-library `unittest`, including `IsolatedAsyncioTestCase` for asynchronous behavior. Name files and methods `test_*`. Runtime checks require a completed installation; page regressions use Node.js assertions. No numerical coverage threshold is configured. Add focused regressions for changed behavior, mocking paid APIs and QQ delivery.

## Commit & Pull Request Guidelines

Follow the README's `<type>: <summary>` format, such as `feat: add reproducible Windows deployment` or `fix: 修复语音重复发送`. Explain behavior changes, verification results, and related issues in pull requests. Include screenshots for GUI changes. For bug fixes, describe the cause and solution. After modifying project code, finish with a suggested commit message.

## Security & Configuration

`dafeiyu-companion` is the private source repository. Keep passwords, API keys, local caches, and generated dependencies in the separate runtime directory and out of Git. Use the external upstream directory only as a reference; apply intended upstream changes to this repository before deployment.

Keep credentials, QQ identities, runtime data, and backups out of Git; never force-add ignored files. Keep private template fields empty. Update release allowlists when introducing public paths, and preserve dependency hashes and third-party attribution.
