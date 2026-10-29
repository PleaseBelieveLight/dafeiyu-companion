# 来源、许可与素材署名

本项目是 Windows 部署与兼容补丁整合，不代表这些上游项目的官方发行版。下载的依赖不提交到本仓库；安装后的各组件保留其上游许可文件和源码。锁定提交见
`dependencies.lock.json`。

| 组件                   | 来源与许可信息                                                                            | 本仓库内容                                                                               |
|------------------------|-------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| 大肥鱼参考项目         | https://github.com/whyever77/dafeiyu-astrbot ，MIT                                        | 两个插件、公开人格/手册、浏览器/ASR 接线及本地修改；许可在 `LICENSES/dafeiyu-MIT.txt`    |
| AstrBot                | https://github.com/AstrBotDevs/AstrBot ，AGPL-3.0-or-later                                | 已修改核心文件与语音协议补丁；完整上游源码由安装器下载                                   |
| LivingMemory           | https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory ，见上游 LICENSE | 安装器获取                                                                               |
| 主动聊天插件           | https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat ，见上游 LICENSE           | 兼容与发送补丁，上游源码由安装器获取                                                     |
| 图片/视频解析          | https://github.com/Zhalslar/astrbot_plugin_parser                                         | 安装器获取                                                                               |
| 点歌                   | https://github.com/Zhalslar/astrbot_plugin_music                                          | 安装器获取                                                                               |
| 戳一戳                 | https://github.com/Zhalslar/astrbot_plugin_pokepro                                        | 安装器获取                                                                               |
| 表情制作               | https://github.com/Zhalslar/astrbot_plugin_memelite                                       | 安装器获取                                                                               |
| 表情收集               | https://github.com/nagatoquin33/astrbot_plugin_stealer                                    | 安装器获取                                                                               |
| 万象画卷               | https://github.com/diaomin66/astrbot_plugin_omnidraw                                      | 安装器获取；本仓库包含配置页兼容与自拍上下文的少量校验替换、新增测试，不包含完整上游源码 |
| PhoneMCP               | https://github.com/kengerlwl/phoneMcp ，上游 pyproject.toml 声明 MIT                      | 输入、应用列表与现场音频兼容补丁；完整上游源码由安装器获取                               |
| NapCatQQ               | https://github.com/NapNeko/NapCatQQ                                                       | 安装器获取；不包含部署者的 QQ 登录状态                                                   |
| FFmpeg                 | https://ffmpeg.org/ ，Windows 构建来自 https://github.com/BtbN/FFmpeg-Builds              | 安装时下载，保留构建内许可证；不上传可执行文件                                           |
| Ollama                 | https://github.com/ollama/ollama                                                          | 优先使用本机安装，缺失时下载固定版本；不上传模型权重                                     |
| Android platform-tools | https://developer.android.com/tools/releases/platform-tools                               | 安装时从 Google 下载，保留包内许可                                                       |
| ADBKeyboard            | https://github.com/senzhk/ADBKeyBoard                                                     | 安装时下载固定提交 APK；用户主动运行准备脚本后才装到专用模拟器                           |

## 默认人物参考图

`assets/selfie-reference.png` 来自大肥鱼原项目的 v2 状态立绘，原作者 **Sutera-Diffusus**
，上游 https://github.com/Sutera-Diffusus/dsh-whale-musume （MIT）。请保留作者、来源与
`assets/ATTRIBUTION.md`。未包含本地使用者聊天中的私有图片。

`assets/selfie-references/` 的 18 张场景参考图原样来自大肥鱼原项目
`assets/avatars/v3-20261025/`。上游标注为用户提供的 ChatGPT 生成图 （2026-10-20 与 2026-10-25 两批），随
MIT 来源项目分发。来源与映射见该目录 README。

新增整合代码与 AstrBot 兼容修改按根目录 AGPL-3.0-or-later
提供；不同组件各自的许可不因整合而被替换。上游仅在元数据声明许可或未附独立许可文件的，以该上游仓库的实际声明为准。
