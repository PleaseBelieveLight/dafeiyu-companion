# 仓库协作规范（中文核对版）

根目录的 [AGENTS.md](../AGENTS.md) 使用英文，规则修改时同步更新本中文核对版。
安装步骤与目录说明见 [README.md](../README.md)。

## 工作区

- 目标环境为 Windows 10/11 x64、Python 3.12。通过 `tools/workspace.py` 解析路径，遵循
  `DAFEIYU_RUNTIME_ROOT` 配置，保持运行目录与源码仓库分离。
- 需要持久保留的修改在本仓库完成，包括 `plugins/` 与 `overlays/`，通过
  `scripts/维护/部署更新.cmd` 部署。外部上游检出目录仅作为可选参考。
- 凭证、QQ 身份、运行数据、备份、生成的依赖和 IDE 设置不纳入 Git。
  `config/settings.example.json` 的私有字段保持为空；不强制添加被忽略的文件，
  不在共享规范中写入本机专属路径或个人偏好。
- 整理源码不得清空、迁移或自动重新导入运行数据。

## 代码修改

- 遵循相邻代码、`pyproject.toml` 和 `.gitattributes`，补丁保留上游风格。
  Python、JSON、Markdown 使用 LF，`.cmd`、`.ps1` 使用 CRLF。
- Windows 入口统一调用 `tools/run.cmd`。根目录只保留启动、停止、状态和配置入口，
  其他脚本归入 `scripts/`，不在运行目录生成 CMD 副本。
- 保留依赖哈希与第三方署名。新增公开根文件或目录时，更新 `tools/check_release.py`
  中的源码发布白名单。

## 文档维护

- 根目录 `README.md` 保持精简。除非明确要求调整，否则仅保留以下二级章节，顺序固定：
  `项目简介`、`核心功能`、`部署说明`、`核心目录架构`、`来源与许可`。
  详细配置、开发、运维与验收说明放在 `docs/`。
- 阅读文档放在 `docs/`，由 `docs/README.md` 导航；程序输入放在 `content/`。
  人格、知识源和验收素材即使使用 Markdown，也属于程序输入。
  在 `content/README.md` 记录读取方、触发时机及修改影响，包括可选模板是否自动读取。
- 启动、知识库初始化和验收工具必须能在没有 `docs/` 时正常工作，文档检查与发布扫描除外。
  同时供人阅读和程序使用的内容只在 `content/` 维护一份，由 `docs/` 链接。
- 优先更新已有文档。单篇文档放在 `docs/` 顶层，确有需要时才建立带简短索引的主题目录。
  避免重复正文与不必要的嵌套，区分历史验收依据与当前行为。
- 插件 README 保留在 `plugins/<插件名>/README.md`，供 AstrBot 后台展示，不作为机器人知识输入。
  脚本说明、素材署名和许可证随各自组件保留。
- 移动或重命名文件时，同步更新链接、索引、脚本读取路径与知识库导入路径。
  修改运行资料前先核对读取方，并更新资料清单。

## 验证

在仓库根目录使用 PowerShell 运行相关检查：

```powershell
py -3.12 -m unittest discover -s tests -v
py -3.12 tools/check_release.py
py -3.12 -m compileall -q tools plugins overlays
```

使用 `unittest` 编写有针对性的回归，异步行为使用 `IsolatedAsyncioTestCase`，
模拟付费 API 与 QQ 投递。Ruff 可用时运行 `ruff check .`。
运行环境回归使用 `python tools/launch.py verify`，需要已完成安装。
修改文档后检查本地链接。

## 交付

- 提交信息遵循 Git 历史的 `<type>: <summary>` 格式，例如 `fix: 修复语音重复发送`。
- PR 说明行为变化与验证结果；界面修改附截图。
- 修复 BUG 后概括原因与修复方法；修改项目代码后，在任务结果中给出建议提交信息。
