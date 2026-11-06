# 对话上下文与用量

## 聊天上下文

OpenAI 兼容聊天适配器在发送请求前整理工作上下文。旧对话按完整轮次增量摘要，近期原文、当前媒体、工具调用及结果一并提供。摘要只用于出站请求，原始聊天仍保存在会话记录中。

默认 DeepSeek 聊天节点使用以下 `companion_context` 配置。安装和部署会补齐缺失的配置对象，已有自定义值优先。

```json
{
  "enabled": true,
  "input_budget": 16000,
  "recent_rounds": 12,
  "summary_max_tokens": 900,
  "summary_input_chars": 24000,
  "route_simple_chat_tools": true
}
```

- `input_budget` 是估算输入的摘要触发线。当前附件、工具说明或等待摘要补齐的历史都可能让实际输入超过该值。
- `recent_rounds` 控制最近原文的保留范围，旧记录边界以四轮为一批推进。一次请求最多推进一次增量摘要，摘要尚未补齐或失败时继续使用原历史。
- `route_simple_chat_tools` 仅缩减问候等明确的社交短句使用的工具。图片、任务和确认句继续使用会话可用的工具。

## 查看用量

在源码根目录执行：

```powershell
python tools/launch.py chat-usage --days 7
```

结果按调用来源、提供商和模型汇总。覆盖 AstrBot OpenAI 兼容聊天适配器和浏览器内层的 SDK 调用，TTS、生图及其他未接入的调用由各服务统计。账本位于运行目录的 `astrbot/data/plugin_data/dafeiyu_context/context.db`。

`attempts` 包含请求尝试和重试。输入、缓存拆分或流式结果缺失时，查看对应的 `unknown_*` 和 `uncertain_attempts`。比较前后用量时使用同一统计窗口与相同来源，费用以供应商账单为准。

## 排查入口

| 现象 | 核对项 |
| --- | --- |
| 用量显示未就绪 | 部署更新后，等已接入的提供商完成一次请求，再确认使用的是对应运行目录。 |
| 长会话用量仍偏高 | 核对生效聊天节点的 `companion_context`、当前附件和 `context_summary` 调用。首次补齐长会话摘要会增加一些调用。 |
| 回复引用旧话题 | 核对最近用户原话、自动长期记忆召回及本轮注入的资料。纯图和没有独立主题的短追问会跳过自动长期记忆检索，明确回忆仍可使用召回工具。 |
| 修改源码人格后回复未变化 | 按[脚本使用说明](../scripts/README.md)预览并同步运行态人格，再看同步后的新对话。 |

实现和边界见[出站上下文处理](../overlays/astrbot/astrbot/core/utils/chat_efficiency.py)、[用量统计](../tools/chat_usage.py)。
