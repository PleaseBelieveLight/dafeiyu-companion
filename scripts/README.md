# Windows 脚本使用说明

所有入口都在 `dafeiyu-companion` 项目内，双击即可使用；不要在 `.local` 运行目录放置副本。

| 位置                | 脚本                                           | 用途                         |
|---------------------|------------------------------------------------|------------------------------|
| 项目根目录          | `启动全部.cmd`、`停止全部.cmd`、`检查状态.cmd` | 日常服务管理                 |
| 项目根目录          | `配置向导.cmd`                                 | 修改账号、模型及功能配置     |
| `scripts/安装配置/` | `安装.cmd`                                     | 首次安装依赖和运行环境       |
| `scripts/安装配置/` | `打开NapCat.cmd`                               | 打开 QQ 登录界面             |
| `scripts/安装配置/` | `准备记忆模型.cmd`                             | 下载、准备记忆模型           |
| `scripts/维护/`     | `备份.cmd`                                     | 备份本地配置和运行数据       |
| `scripts/维护/`     | `部署更新.cmd`                                 | 将主项目的补丁部署到运行目录 |
| `scripts/维护/`     | `同步人格知识库.cmd`                           | 同步人格和知识文档           |
| `scripts/维护/`     | `修复QQ连接.cmd`                               | 检查并修复 QQ 连接           |
| `scripts/维护/`     | `验证安装.cmd`                                 | 检查依赖和运行回归测试       |
| `scripts/扩展功能/` | `启动虚拟手机.cmd`                             | 启动已配置的专用模拟器       |

入口统一调用 `tools/run.cmd`，由它选择 Python 3.12、设置工作目录并转发参数到 `tools/launch.py`。
命令行同样可以使用，例如在项目根目录执行：

```powershell
.\scripts\安装配置\安装.cmd --skip-model
.\scripts\维护\部署更新.cmd
python tools/launch.py chat-usage --days 7
python tools/launch.py sync-persona --apply
python tools/launch.py request-trace status
```

任务完成后保留窗口供查看结果；配置向导、NapCat 成功打开后自动关闭命令窗口。 自动化调用可先设置
`$env:DAFEIYU_NO_PAUSE = '1'` 关闭暂停；脚本会保留任务的退出码。 如需自定义运行目录，设置
`DAFEIYU_RUNTIME_ROOT`，所有入口都会继承该设置。

## 不安装本地记忆模型

只需 DeepSeek 基础聊天时，可运行：

```powershell
.\scripts\安装配置\安装.cmd --skip-ollama
```

此选项跳过 Ollama 和 bge-m3，在本机配置中保存 `local_memory_enabled: false`；
不启动 Ollama，禁用长期记忆插件、知识库检索及表情包向量检索。普通聊天历史和人格仍可使用，
已有记忆数据不会删除。`--skip-model` 仅推迟下载 bge-m3，仍会安装并启动 Ollama。

以后要恢复本地记忆，先停止服务，不带 `--skip-ollama` 重新运行安装脚本，
再将运行目录 `config/settings.json` 的 `local_memory_enabled` 改为 `true`，
通过配置向导保存并启动，并在 AstrBot 插件管理中启用长期记忆插件。

## 本地运行与数据目录

默认目录为项目文件夹内的 `.local/`。它整体排除在 Git 和源码发布包之外，内部结构保持不变：

| 子目录 | 内容 |
|--------|------|
| `astrbot/`、`napcat/`、`phone/`、`browser/` | 已安装组件及其数据；聊天、记忆和知识库位于 `astrbot/data/` |
| `runtime/`、`bin/`、`models/` | Python 环境、工具和模型 |
| `config/`、`appdata/` | 本机配置、密钥及应用数据 |
| `cache/`、`downloads/`、`temp/` | 缓存、安装下载和临时文件 |
| `logs/`、`run/`、`backups/` | 日志、进程状态和备份 |

`.local` 包含需要长期保留的私人数据，不要把整个目录作为缓存删除。更新源码时保留它，
先用 `scripts/维护/备份.cmd` 备份配置和运行数据；重要备份还应复制到项目之外。
Git 清理时不要使用会删除忽略文件的 `git clean -fdx`。

`DAFEIYU_RUNTIME_ROOT` 仍可指定项目外的目录，也可指定 `.local` 下的子目录；
相对路径以项目根目录为基准。项目根目录、其上级和其它源码子目录不能作为运行目录。

旧版使用项目旁的 `Dafeiyu/`。更新源码不会自动搬迁或重新导入数据；发现旧环境时，
启动和安装入口会提示迁移，避免误装出第二套环境。尚未迁移时，可在 PowerShell 中临时指定旧目录：

```powershell
$env:DAFEIYU_RUNTIME_ROOT = Join-Path (Split-Path $PWD -Parent) 'Dafeiyu'
.\检查状态.cmd
```

迁移需先停止服务并备份，再移动完整运行目录，同时更新配置中的绝对路径和 Python 环境的脚本入口；
只拖动文件夹不能保证环境可用。聊天历史中的本地附件也可能引用旧路径，需保留可用的引用。
迁移验证完成后，清除旧的 `DAFEIYU_RUNTIME_ROOT` 设置，日常入口即可使用默认 `.local`。

`chat-usage` 只读查看已接入聊天模型与浏览器的用量；`sync-persona` 默认预览差异，
加 `--apply` 才会备份并同步源码人格正文。它保留后台工具、技能与开场配置；
`request-trace` 提供完整模型请求与运行事件的开关、查看和导出，首次部署默认关闭。
使用方式与记录边界见[模型请求与运行调试记录](../docs/调试记录.md)。
