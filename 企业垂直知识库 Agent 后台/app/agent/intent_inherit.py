"""短追问的意图继承（v1.12）。

## 问题

多轮 RAG 对话里，用户常用极短的指代型追问：

    用户：研发部住宿标准        -> route=rag
    用户：去成都呢？            -> route=direct  ❌ 没有任何检索
    用户：刚才那份制度什么时候修订的？ -> 也可能被判 direct

`direct` 路由不调用任何工具，于是模型只能靠历史消息"背"上一轮的答案；
一旦上文没提过该问题（比如首轮就问「去成都呢？」），就只能答"没有资料"。
本质是**路由器只看单轮问题，看不到对话上下文**。

## 做法

不问 LLM，直接用规则拦截（短路）：

    上一轮 route ∈ {rag, db_query}
      且 本轮问题足够短（去空白标点后 ≤ 12 字）
      且 出现指代/延续信号（呢/那/这个/还有/换成…）或极短疑问式（≤8 字含 多少/怎么/谁…）
    => 直接继承上一轮的 route，跳过 LLM 分类

## 为什么用规则而不是让 LLM 带上文再判一次

1. **确定性**：同样的对话历史必须得到同样的路由，否则评测无法复现；
2. **成本与延迟**：短追问往往一轮里问好几遍，多一次 LLM 调用纯属浪费；
3. **可控性**：规则命中/未命中都能在日志里说清原因（`reason`），出问题能定位。

## 刻意不继承的情形

- `report` / `parallel_compare`：这两路会产出大量结构化内容，短追问几乎不可能是
  「再生成一份报表」，继承反而制造垃圾产出；
- 含寒暄且无疑问词（"你好呀，今天天气不错"）：这是闲聊，不该被拽回检索；
- 首轮（无上一轮 route）：没有可继承的意图，保持 LLM 判断。
"""
from __future__ import annotations

import re

#: 可以被子问题继承的路由。report/parallel_compare 刻意排除（见模块 docstring）
INHERITABLE_ROUTES = ("rag", "db_query")

#: 短追问长度上限（去空白与标点后按字符数计）
MAX_SHORT_LEN = 12
#: 「极短疑问式」的长度上限（如「多少钱」「谁审批」）
MAX_TINY_LEN = 8

#: 指代 / 延续信号：出现任意一个即可认为是接着上一轮在问
CONTEXTUAL_MARKERS = (
    "呢", "那", "这个", "那个", "这些", "那些", "它", "他", "她",
    "还有", "以及", "上面", "刚才", "前面", "前者", "后者",
    "继续", "再说", "再问", "详细", "具体", "其中", "同样", "也是",
    "的话", "换成", "改成", "如果", "另", "此外", "顺便",
)

#: 「指向型」疑问词：短句里出现它，说明问的是上一轮已经提到的对象
#: （谁 / 哪 / 几 / 多少 / 多久 / 是否 …），适合继承。
POINTING_QUERY_MARKERS = (
    "谁", "哪", "几", "多少", "多久", "多大", "是否",
    "有没有", "能不能", "可不可以",
)

#: 纯寒暄：这类短句不是追问，别拽回检索
GREETING_MARKERS = (
    "你好", "您好", "谢谢", "在吗", "早上好", "中午好", "下午好", "晚上好",
    "嗨", "hello", "hi", "辛苦", "好的", "收到", "明白", "再见",
)

_PUNCT_RE = re.compile(r"[\s，。？！、,.?!;；:：\"'“”‘’()（）\[\]【】…~～]+")


def visible_len(question: str) -> int:
    """去掉空白与标点后的可见字符数（判断「短」的唯一口径）。"""
    return len(_PUNCT_RE.sub("", question or ""))


def decide_inherit(last_route: str | None, question: str) -> tuple[str | None, str]:
    """判定是否继承上一轮路由。

    返回 `(route_to_inherit | None, reason)`：
    - route 非 None 表示命中继承，调用方应直接采用、跳过 LLM 分类；
    - reason 只用于日志，说明命中或未命中的依据。
    """
    if not last_route:
        return None, "no_last_route"
    if last_route not in INHERITABLE_ROUTES:
        return None, f"last_route_not_inheritable:{last_route}"

    text = (question or "").strip()
    size = visible_len(text)
    if size == 0:
        return None, "empty_question"
    if size > MAX_SHORT_LEN:
        return None, f"too_long:{size}>{MAX_SHORT_LEN}"

    lowered = text.lower()
    has_pointing = any(k in text for k in POINTING_QUERY_MARKERS)
    has_greeting = any(k in lowered for k in GREETING_MARKERS)
    has_context = any(k in text for k in CONTEXTUAL_MARKERS)

    # 纯寒暄（哪怕很短）不继承
    if has_greeting and not has_pointing:
        return None, "greeting_without_query"

    if has_context:
        return last_route, f"contextual_marker(len={size})"
    # 极短且指向型疑问（「谁审批」「多少钱」）：没有指代词也视为追问同一主题
    if size <= MAX_TINY_LEN and has_pointing:
        return last_route, f"tiny_pointing_query(len={size})"

    return None, f"no_signal(len={size})"
