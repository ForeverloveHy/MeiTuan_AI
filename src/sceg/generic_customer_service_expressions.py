"""Task-agnostic customer-service expression cues used by local judges.

This module is intentionally limited to cross-domain Chinese dialogue forms:
turn taking, polite continuation, short acknowledgement, awareness questions,
and generic UI/process words.  It must not contain business nouns, product
names, sample ids, domain labels, or injected negative-answer text.

Concrete business facts still come from the LLM schema / graph.  The local
executor only uses these cues to tolerate normal customer-service paraphrases.
"""

from __future__ import annotations

# Awareness / confirmation question forms, e.g. "do you know / are you aware".
INQUIRY_MARKERS: tuple[str, ...] = ("吗", "是否", "是不是", "有没有")
AWARENESS_VERBS: tuple[str, ...] = ("知道", "了解", "知情", "确认")

# Generic structural words often used by LLM when it describes a UI/process
# action.  These are not task labels; they help avoid matching relaxed short
# predicates on object names alone.
STRUCTURAL_SIGNAL_MARKERS: tuple[str, ...] = (
    "选项", "显示", "展示", "页面", "类型", "方式", "规则", "流程", "入口", "设置", "配置", "时间",
)

# User utterances that clearly permit a brief continuation after a soft
# inconvenience / busy signal.
USER_CONTINUE_CUES: tuple[str, ...] = (
    "你说", "您说", "说吧", "继续", "接着", "讲重点", "挑重点", "快点说", "我在听",
    "可以说", "能说", "简短说", "说重点", "长话短说", "说一下", "说完", "别漏",
)

# User utterances that mean the conversation should not continue now.
USER_STOP_CUES: tuple[str, ...] = (
    "别说", "不要说", "先挂", "挂了", "回头", "稍后", "没空", "不方便接",
)

# Generic turn-management cues in schemas and terminal policies.
TURN_MANAGEMENT_CUES: tuple[str, ...] = (
    "简短", "稍后", "回电", "自然结束", "结束通话", "挂断", "不打扰", "忙碌", "不方便",
)
HARD_TERMINAL_CUES: tuple[str, ...] = ("立即结束", "挂断", "安全", "不能继续", "禁止继续")

# Common short acknowledgement forms for cross-turn "assistant asks + user
# confirms" evidence groups.
SHORT_ACK_NEGATIVE_MARKERS: tuple[str, ...] = ("不是", "不对", "没有", "没", "否", "别", "不要")
SHORT_ACK_AFFIRMATIVE_MARKERS: tuple[str, ...] = (
    "是", "对", "嗯", "恩", "本人", "我", "没错", "可以", "行", "好", "你说", "您说",
)
