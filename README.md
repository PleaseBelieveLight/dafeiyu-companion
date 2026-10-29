# 大肥鱼 QQ 机器人

## 项目简介

基于大肥鱼的 Windows 本地部署项目， 整合
AstrBot、NapCat、配置向导和兼容补丁，提供带人格、记忆与多媒体交互的 QQ 机器人。

仓库提供源码与安装工具，依赖和运行数据在安装时生成；QQ 账号、模型 API Key 需自行准备。

## 核心功能

| 功能             | 说明                                                            |
|------------------|-----------------------------------------------------------------|
| QQ 互动          | 私聊、群聊、表情收集与发送、点歌、戳一戳，支持好友和群白名单    |
| 多媒体理解与回复 | 看图、视频解析、语音识别和语音回复                              |
| 人格与配图       | 自定义人格、人设自拍、生图，以及跳舞、摆姿势等动作配图          |
| 记忆与主动互动   | 基于 Ollama + bge-m3 的长期记忆与知识库，支持主动聊天和定时任务 |
| 浏览器与虚拟手机 | 只读网页浏览，以及通过专用 Android 模拟器操作应用、获取现场声音 |

视觉、语音和生图需配置对应模型服务；浏览器需本机 Chrome 或 Edge，手机功能需另备模拟器与 ADB。
视频生成需另配视频服务，默认不包含。

## 部署说明

环境要求： **Windows 10/11 x64、Python 3.12 x64（含 pip、Tcl/Tk）**，建议预留 **15–20 GB**
空间，首次安装需联网。Python 可从[官方下载页](https://www.python.org/downloads/windows/)获取。

1. 克隆或解压仓库，例如放在 `D:\dafeiyu-companion`。
2. 双击 `scripts/安装配置/安装.cmd`，下载依赖并创建运行环境。
3. 打开 `配置向导.cmd`，填写主人 QQ、机器人 QQ、聊天模型 API Key 和好友／群白名单；
   按需配置视觉、语音、生图等功能，然后点击“保存配置并启动”。
4. 打开 `scripts/安装配置/打开NapCat.cmd`，用手机 QQ 扫码登录机器人账号并完成验证。
5. 私聊机器人发送“在吗”确认连接；需要普通聊天语音时发送 `/语音开`，关闭用 `/语音关`。

日常使用根目录的 `启动全部.cmd`、`停止全部.cmd`、`检查状态.cmd` 管理服务， 通过 `配置向导.cmd`
调整配置。面板账号与密码可在向导“管理入口”查看。 更新插件或补丁后运行 `scripts/维护/部署更新.cmd`
；更多操作见[脚本使用说明](scripts/README.md)。

## 核心目录架构

```text
dafeiyu-companion/          源码与安装工具
├─ tools/                  安装、配置向导、服务管理与本地 MCP 工具
├─ scripts/                安装配置、维护、扩展功能脚本
├─ plugins/                随项目分发的插件及本地修改
├─ overlays/               部署到上游组件的兼容补丁
├─ content/                人格设定与知识库文档
├─ assets/                 图片素材与署名
├─ config/                 空密钥配置模板
├─ tests/                  源码检查与运行回归测试
├─ dependencies.lock.json  上游版本与完整性校验
└─ requirements/           Python 依赖锁定文件
```

所有 `.cmd` 入口均从源码项目使用；插件与补丁在本项目修改，再通过部署更新同步到运行目录。

## 来源与许可

本项目参考于 [whyever77/dafeiyu-astrbot](https://github.com/whyever77/dafeiyu-astrbot)，
整合修改采用 [AGPL-3.0-or-later](LICENSE)，各上游组件保留各自许可。
完整来源与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [LICENSES/](LICENSES)，
图片素材署名见 [assets/ATTRIBUTION.md](assets/ATTRIBUTION.md)。
