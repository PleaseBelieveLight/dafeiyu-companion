# 仓库协作规范（中文核对版）

根目录的 [AGENTS.md](../AGENTS.md) 使用英文，规则修改时同步更新本中文核对版。
项目介绍与首次安装步骤见 [README.md](../README.md)。

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

- 根目录 `README.md` 是面向无编程经验读者的 GitHub 项目首页，保持精简。
  按读者需求组织章节，说明项目用途、安装准备、上手步骤和求助入口。
  技术细节链接到各自的维护指南，版权、许可证和第三方署名与项目声明保持一致。
- 每个主题维护一份正文。插件用法和排障放在 `plugins/<插件名>/README.md`，供 AstrBot 后台展示。
  脚本说明、素材署名和许可证随各自组件保留。跨组件指南和进行中的方案放在 `docs/`，
  `docs/README.md` 用简短索引链接到各说明的维护入口。
- 优先更新已有文档。只有独立且会反复查阅的需求或进行中的方案才另建文档。单篇放在 `docs/` 顶层，
  多篇长期维护的文档确有分组需要时，再建立带简短索引的子目录。已完成的阶段、一次性排查和测试结果
  记录在提交说明、PR 或 Git 历史。方案收尾时，把长期有效的操作、已知限制和待验收项归入对应说明，
  再移除重复文档。
- 程序输入放在 `content/`，包括用 Markdown 编写的人格、知识源和验收素材。
  在 `content/README.md` 记录读取方、触发时机及修改影响，包括可选模板是否自动读取。
- 启动、知识库初始化和验收工具必须能在没有 `docs/` 时正常工作，文档检查与发布扫描除外。
  同时供人阅读和程序使用的内容只在 `content/` 维护一份，由 `docs/` 链接。
- 依据当前源码核对文档中的命令、路径和默认值。保留的历史测量标明日期与范围。
  移动、重命名或移除文件时检查引用，同步更新链接、索引、脚本读取路径与知识库导入路径。
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
