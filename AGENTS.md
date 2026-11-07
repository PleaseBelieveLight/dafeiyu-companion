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

- Write the root `README.md` as a concise GitHub landing page for readers without programming
  experience. Choose headings that help readers understand what the project does, what they need
  to install it, how to try it, and where to get help. Link to the owning guides for technical
  detail. Keep copyright, license, and third-party attribution aligned with the project notices.
- Give each topic one maintained guide. Put plugin usage and troubleshooting in its
  `plugins/<plugin_name>/README.md` for the AstrBot admin UI. Keep script guides, asset attribution,
  and licenses with their components. Use `docs/` for project-wide guides and active plans, with
  `docs/README.md` as a short index that links to the owning guides.
- Update an existing guide first. Add a standalone page only for a distinct recurring need or
  an active plan. Keep pages at the top of `docs/`. Add an indexed subdirectory only when several
  maintained pages need it. Record completed phases, one-off investigations, and test runs in
  commit messages, PRs, or Git history. When a plan closes, move durable instructions, known
  limits, and open acceptance items into the owning guide, then retire the redundant pages.
- Keep program inputs in `content/`, including personas, knowledge sources, and acceptance
  materials written in Markdown. Record consumers, read triggers, and change impacts in
  `content/README.md`, including whether optional templates are read automatically.
- Startup, knowledge initialization, and acceptance tools must work without `docs/`; documentation
  checks and release scans are exceptions. Keep content used by both people and programs once in
  `content/`, with links from `docs/`.
- Verify documented commands, paths, and defaults against the current source. Label any retained
  historical measurements with their date and scope. When moving, renaming, or removing files,
  check references and update links, indexes, script readers, and knowledge import paths together.
  Check consumers before changing runtime inputs and update their inventory.

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
