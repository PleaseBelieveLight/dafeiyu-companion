# Repository Guidelines

Keep this document in English and update the [Chinese review copy](docs/AGENTS.zh-CN.md) together.
See [README.md](README.md) for setup and directory layout.

## Workspace

- Target Windows 10/11 x64 and Python 3.12. Resolve paths through `tools/workspace.py` and respect
  `DAFEIYU_RUNTIME_ROOT`; keep the runtime separate from the source repository.
- Make persistent changes in this repository, including `plugins/` and `overlays/`.
  Deploy with `scripts/维护/部署更新.cmd`. External upstream checkouts are optional references.
- Keep credentials, QQ identities, runtime data, backups, generated dependencies, and IDE settings
  out of Git. Leave private fields in `config/settings.example.json` empty; do not force-add
  ignored files or put machine-specific paths and personal preferences in shared guidance.
- Source cleanup must not erase, migrate, or automatically reimport runtime data.

## Code Changes

- Follow adjacent code, `pyproject.toml`, and `.gitattributes`; preserve upstream style in overlays.
  Use LF for Python/JSON/Markdown and CRLF for `.cmd`/`.ps1`.
- Route Windows entries through `tools/run.cmd`. Keep only start, stop, status, and configuration
  entries at the root; group other scripts under `scripts/`. Do not generate runtime CMD copies.
- Preserve dependency hashes and third-party attribution. Update the source release allowlist in
  `tools/check_release.py` when adding public root files or directories.

## Documentation

- Keep the root `README.md` concise. Unless explicitly requested otherwise, retain these H2
  sections in order: `项目简介`, `核心功能`, `部署说明`, `核心目录架构`, `来源与许可`.
  Put detailed configuration, development, operations, and validation notes in `docs/`.
- Keep human-readable guides in `docs/`, indexed by `docs/README.md`, and program inputs in
  `content/`. Personas, knowledge sources, and acceptance materials remain program inputs even
  when written in Markdown. Record consumers, read triggers, and change impacts in
  `content/README.md`, including whether optional templates are read automatically.
- Startup, knowledge initialization, and acceptance tools must work without `docs/`; documentation
  checks and release scans are exceptions. Keep content used by both people and programs once in
  `content/`, with links from `docs/`.
- Update existing documents first. Keep standalone documents at the top of `docs/`; create topic
  folders with a short index only when needed. Avoid duplicate explanations and unnecessary
  nesting; distinguish historical validation from current behavior.
- Keep plugin READMEs in `plugins/<plugin_name>/README.md` for the AstrBot admin UI, separate from
  bot knowledge inputs. Keep script guides, asset attribution, and licenses with their components.
- When moving or renaming files, update links, indexes, script readers, and knowledge import paths
  together. Check consumers before changing runtime inputs and update their inventory.

## Validation

Run relevant checks from the repository root in PowerShell:

```powershell
py -3.12 -m unittest discover -s tests -v
py -3.12 tools/check_release.py
py -3.12 -m compileall -q tools plugins overlays
```

Use `unittest` (`IsolatedAsyncioTestCase` for async behavior) for focused regressions; mock paid
APIs and QQ delivery. Run `ruff check .` when available. Installed runtime checks use
`python tools/launch.py verify` and require a completed installation. Verify local links after
documentation changes.

## Delivery

- Use `<type>: <summary>` commit messages, following Git history, e.g. `fix: 修复语音重复发送`.
- Explain behavior changes and verification results in PRs; include screenshots for GUI changes.
- For bug fixes, summarize the cause and solution. After changing project code, finish with a
  suggested commit message.
