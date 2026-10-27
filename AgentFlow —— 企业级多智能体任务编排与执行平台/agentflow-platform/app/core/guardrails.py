"""AI 输入安全护栏（任务 10：防提示词注入与越狱）。

分层防御：

1. ``sanitize_user_input``——用户输入层轻量清洗（建任务等入口调用）。
   正则拦截常见越狱特征词（中英文），命中即抛 ``AppException(INVALID_INPUT)``。
2. ``IntentGuardrail``——全局意图护栏兜底（改进 3）。
   规则引擎版：识别"破坏性操作 + 强制执行工具指令"类攻击意图；
   接口按可替换设计——后续可平滑换成小模型分类器，``check`` 签名不变。

设计约定：

- 收编 ``task_service.INJECTION_BLACKLIST``（PRD FR-001 验收 2）全部条目，
  并从"子串匹配"升级为"正则匹配"（可挡大小写、空格变体、中英混写）；
- 错误码走 W3 口径（字符串码 + 显式 HTTP 状态），不修改 W1 冻结的 ErrorCode 枚举；
- 拦截只针对**用户可自由输入的目标文本**；检索资料等外部内容走 prompts.py
  的 ``wrap_untrusted`` 物理隔离（防间接注入），两道防线互补。
"""

from __future__ import annotations

import re

from app.core.exceptions import AppException
from app.core.logging import get_logger

logger = get_logger(__name__)

INVALID_INPUT = "INVALID_INPUT"    # 输入包含违规内容（越狱特征词）
INTENT_BLOCKED = "INTENT_BLOCKED"  # 输入疑似攻击意图（意图护栏兜底）

# ---------------- 越狱特征词（正则，大小写不敏感） ----------------
# 收编 task_service.INJECTION_BLACKLIST（PRD FR-001）全部条目并扩展变体覆盖
JAILBREAK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"忽略(之前|以上|前面|先前)(的)?(所有|全部)?(系统)?指令",
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
        r"system\s*prompt",
        r"你(现在|从现在起)?是一个?没有(任何)?限制的\s*AI",
        r"没有(任何)?限制的\s*AI",
        r"开发者模式",
        r"developer\s*mode",
        r"\bDAN\s*mode\b",
        r"越狱",
        r"进入(上帝|神)模式",
        r"你已(经)?(解除|摆脱)(了)?(所有)?(限制|约束)",
        r"pretend\s+(you\s+)?(are|to\s+be)\s+an?\s+(unrestricted|unfiltered)",
    )
)

# ---------------- 攻击意图特征（IntentGuardrail 规则引擎） ----------------
# 特征：指令式动词 + 破坏性/越权动作的组合，而非孤立关键词（降低误报）
_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(立即|马上|立刻)?(执行|调用|运行).{0,8}(删除|清除|清空|drop\s+table|truncate)",
        r"(删除|清空)(所有|全部).{0,6}(数据|表|记录|文件)",
        r"(绕过|跳过|禁用)(审批|审核|权限|校验|安全检查)",
        r"(读取|导出|泄露)(所有|全部)?(用户|客户)?(密码|密钥|凭据|隐私)",
        r"格式化(磁盘|硬盘|c盘)",
        r"rm\s+-rf\s+/|del\s+/[sqs]",
    )
)


def sanitize_user_input(text: str) -> str:
    """用户输入层轻量清洗：命中越狱特征词即拒绝，返回 strip 后的原文。

    抛 ``AppException(INVALID_INPUT)``（HTTP 400），提示"输入包含违规内容"。
    """
    normalized = (text or "").strip()
    for pattern in JAILBREAK_PATTERNS:
        match = pattern.search(normalized)
        if match:
            logger.warning(
                "input_sanitizer_blocked",
                extra={"pattern": pattern.pattern, "excerpt": normalized[:60]},
            )
            raise AppException(
                INVALID_INPUT,
                "输入包含违规内容",
                status_code=400,
                detail={"reason": "JAILBREAK_PATTERN", "pattern": pattern.pattern},
            )
    return normalized


class IntentGuardrail:
    """全局意图护栏兜底（任务 10 改进 3）。

    当前实现：轻量规则引擎（正则特征组合，非孤立关键词，降低误报）。
    预留演进路径：把 ``check`` 内部替换为小模型意图分类器，签名与调用方不变。
    """

    async def check(self, text: str, *, source: str = "task_goal") -> None:
        """命中攻击意图 → WARNING 日志 + ``AppException(INTENT_BLOCKED)``（400）。"""
        normalized = (text or "").strip()
        for pattern in _INTENT_PATTERNS:
            match = pattern.search(normalized)
            if match:
                logger.warning(
                    "intent_guardrail_blocked",
                    extra={"source": source, "pattern": pattern.pattern, "excerpt": normalized[:60]},
                )
                raise AppException(
                    INTENT_BLOCKED,
                    "输入疑似包含攻击意图，已拦截",
                    status_code=400,
                    detail={"reason": "ATTACK_INTENT", "source": source},
                )


#: 模块级单例——调用方直接 ``await intent_guardrail.check(text)``；
#: 测试可 monkeypatch 或自建实例注入特征
intent_guardrail = IntentGuardrail()

__all__ = [
    "INVALID_INPUT",
    "INTENT_BLOCKED",
    "JAILBREAK_PATTERNS",
    "IntentGuardrail",
    "intent_guardrail",
    "sanitize_user_input",
]
