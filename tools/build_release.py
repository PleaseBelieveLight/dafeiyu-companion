"""Package a committed version tag as a checked Windows source distribution."""

import argparse
import hashlib
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import check_release
from workspace import SOURCE_ROOT

VERSION_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def release_notes(root: Path, tag: str, commit: str) -> str:
    tags = git(root, "tag", "--merged", "HEAD", "--sort=-version:refname").splitlines()
    version = tuple(map(int, tag[1:].split(".")))
    previous = next(
        (
            item
            for item in tags
            if VERSION_TAG.fullmatch(item) and tuple(map(int, item[1:].split("."))) < version
        ),
        None,
    )
    changes = git(
        root,
        "log",
        f"{previous}..HEAD" if previous else "HEAD",
        "--format=- %s (%h)",
        "--no-merges",
    )
    return (
        f"## {tag}\n\n源码提交：`{commit}`。\n\n"
        "## 安装与升级\n\n"
        f"下载 `dafeiyu-companion-{tag}.zip` 和 `SHA256SUMS.txt`，核对 SHA256 后解压。\n"
        "环境要求为 Windows 10/11 x64、Python 3.12 x64，首次安装需要联网。\n"
        "首次安装请阅读包内 README.md；已有部署请先备份，再更新源码并运行 "
        "`scripts/维护/部署更新.cmd`。运行目录、账号配置和持久化数据应保留。\n\n"
        "## 验证范围\n\n"
        "发布流程运行源码测试、分发检查和 Python 语法检查；"
        "QQ 登录、真实模型调用及语音收发需要安装环境验收。"
        "压缩包仅包含源码、安装工具与配套资料，依赖由安装脚本下载。\n\n"
        f"## 更新记录{f'（自 {previous}）' if previous else '（首次发布）'}\n\n"
        f"{changes or '无新增非合并提交。'}\n"
    )


def build_release(tag: str, output: Path, root: Path = SOURCE_ROOT) -> Path:
    if not VERSION_TAG.fullmatch(tag):
        raise ValueError("Release tag must use vMAJOR.MINOR.PATCH, for example v0.1.0")
    root = root.resolve()
    commit = git(root, "rev-parse", "HEAD")
    if git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit:
        raise ValueError("Release tag must point to the checked-out HEAD commit")
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Commit or restore tracked changes before packaging a release")
    problems = check_release.inspect(root)
    if problems:
        raise ValueError("Source distribution check failed:\n" + "\n".join(problems))

    allowed = {path.relative_to(root).as_posix() for path in check_release.source_files(root)}
    files = []
    for entry in git(root, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, _, stage = metadata.split()
        if name not in allowed or mode not in {"100644", "100755"} or stage != "0":
            raise ValueError("Unexpected tracked file or file mode: " + name)
        path = root / name
        if not path.resolve().is_relative_to(root) or any(
            part.is_symlink() or part.is_junction()
            for part in (path, *path.parents)
            if part != root and root in part.parents
        ):
            raise ValueError("Linked paths are not distributable: " + name)
        files.append((name, path))

    output = output.resolve()
    if (
        output == root
        or output in root.parents
        or (
            output.is_relative_to(root)
            and output.relative_to(root).parts[0] in check_release.PUBLIC_DIRS
        )
    ):
        raise ValueError("Release output must be outside public source directories")
    notes = release_notes(root, tag, commit)
    timestamp = max(315532800, int(git(root, "show", "-s", "--format=%ct", "HEAD")))
    date_time = datetime.fromtimestamp(timestamp, timezone.utc).timetuple()[:6]
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"dafeiyu-companion-{tag}.zip"
    for target in (archive, output / "SHA256SUMS.txt", output / "RELEASE_NOTES.md"):
        if target.exists() or target.is_symlink():
            raise FileExistsError("Use an empty output directory: " + str(target))
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, path in sorted(files):
            info = zipfile.ZipInfo("dafeiyu-companion/" + name, date_time=date_time)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, path.read_bytes())
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    (output / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Existing version tag, for example v0.1.0")
    parser.add_argument("--output", type=Path, default=SOURCE_ROOT / "dist")
    args = parser.parse_args()
    try:
        print(build_release(args.tag, args.output))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release packaging failed: {error}\n")


if __name__ == "__main__":
    main()
