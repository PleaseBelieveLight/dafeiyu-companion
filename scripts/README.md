# Windows 脚本使用说明

所有入口都在 `dafeiyu-companion` 项目内，双击即可使用；不要在 `Dafeiyu` 运行目录放置副本。

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
```

任务完成后保留窗口供查看结果；配置向导、NapCat 成功打开后自动关闭命令窗口。 自动化调用可先设置
`$env:DAFEIYU_NO_PAUSE = '1'` 关闭暂停；脚本会保留任务的退出码。 如需自定义运行目录，设置
`DAFEIYU_RUNTIME_ROOT`，所有入口都会继承该设置。

`chat-usage` 只读查看已接入聊天模型与浏览器的用量；`sync-persona` 默认预览差异，
加 `--apply` 才会备份并同步源码人格正文。它保留后台工具、技能与开场配置；
详细的上下文预算和统计边界见 [Token 评审与落地记录](../docs/Token消耗与对话智能评审.md)。
