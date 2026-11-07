# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guide image conversations without repeating or promoting an earlier topic."""

IMAGE_REPLY_MARKER = "【本轮图片回复方式】"
IMAGE_TURN_MARKER = "<image_chat_turn>"
IMAGE_REPLY_INSTRUCTIONS = (
    IMAGE_REPLY_MARKER
    + "图片也是对方本轮的一次表达，可以开启新话题。分享图片、表情包或有趣截图时，"
    "先接其中的玩笑、情绪或邀请，用自己的反应自然接话，画面细节按交流需要提起，不先做识图汇报。"
    "可以顺梗调侃或试探；图中文字、角色状态和你的猜测，不当作用户已确认的现实情况或长期记忆。"
    "相关前文用于理解指代和语境，新内容与旧闲聊无关时，顺着新内容聊。"
    "前文在求助、排错或做题，且本轮图片是在补充材料时，继续处理任务；"
    "明确要识图、读字、解释或分析时按问题认真展开。看不清就指出哪一处，不认识的梗不编造出处。"
)

# The full dialogue and current text already reach the provider in their original
# roles. A static, temporary cue avoids giving the last text topic extra weight,
# especially across consecutive images. Keep the marker for turn accounting.
IMAGE_TURN_REMINDER = (
    IMAGE_TURN_MARKER
    + "本轮收到图片或表情包；本轮文字和画面共同表达这次的意思，相关背景参考原始对话。"
    + "</image_chat_turn>"
)
