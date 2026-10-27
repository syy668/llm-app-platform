"""Trace 全链路日志：LangChain 回调处理器 + 落库。

通过 LangChain 回调挂到 LangGraph 图执行上，不改动路由/工具业务逻辑：
- on_chain_start/end 捕获各节点（router / rag_tool / db_query_tool / report_tool /
  parallel_compare）执行与耗时；
- on_llm_start/end 捕获 token 用量；
- flush() 把采集到的数据写入 trace_logs / trace_tool_calls。

约束：日志写入失败仅打印 error 日志，绝不中断主问答流程。
"""
import logging
import time
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler

from ..config import settings
from ..core import runtime_config
from ..core.database import SessionLocal, engine
from ..llm.truncation import is_truncated_result
from ..models.trace_log import TraceLog
from ..models.trace_tool_call import TraceToolCall
from .tool_context import tool_trace_context_var

logger = logging.getLogger(__name__)

# 图节点名 -> (step_name, tool_name)
_STEP_MAP = {
    "classify": ("router", "route_classifier"),
    "rag": ("rag_tool", "rag_tool"),
    "db_query": ("db_query_tool", "db_query_tool"),
    "report": ("report_tool", "report_tool"),
    "parallel_compare": ("parallel_compare", "parallel_compare"),  # 均 16 字符，未超列宽
}

_tables_ready = False


def ensure_trace_tables() -> None:
    """确保 trace 表存在（幂等，首次调用建表）。"""
    global _tables_ready
    if _tables_ready:
        return
    TraceLog.__table__.create(bind=engine, checkfirst=True)
    TraceToolCall.__table__.create(bind=engine, checkfirst=True)
    _tables_ready = True


def _extract_usage(response) -> dict:
    """从 LLMResult 提取 token 用量，兼容 llm_output 与 usage_metadata 两种结构。"""
    llm_output = getattr(response, "llm_output", None) or {}
    tu = llm_output.get("token_usage") or llm_output.get("usage")
    if tu:
        return {
            "input_tokens": tu.get("prompt_tokens", tu.get("input_tokens", 0)) or 0,
            "output_tokens": tu.get("completion_tokens", tu.get("output_tokens", 0)) or 0,
            "total_tokens": tu.get("total_tokens", 0) or 0,
        }
    for gen_list in getattr(response, "generations", None) or []:
        for gen in gen_list:
            msg = getattr(gen, "message", None)
            if msg is None:
                continue
            um = getattr(msg, "usage_metadata", None)
            if um is not None:
                return {
                    "input_tokens": getattr(um, "input_tokens", 0) or 0,
                    "output_tokens": getattr(um, "output_tokens", 0) or 0,
                    "total_tokens": getattr(um, "total_tokens", 0) or 0,
                }
            rm = getattr(msg, "response_metadata", None) or {}
            tu2 = rm.get("token_usage") or rm.get("usage")
            if tu2:
                return {
                    "input_tokens": tu2.get("prompt_tokens", tu2.get("input_tokens", 0)) or 0,
                    "output_tokens": tu2.get("completion_tokens", tu2.get("output_tokens", 0)) or 0,
                    "total_tokens": tu2.get("total_tokens", 0) or 0,
                }
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


class TraceCallbackHandler(BaseCallbackHandler):
    """采集一次问答过程中的节点与 token 信息，最终由 flush() 落库。"""

    def __init__(self, trace_id, conversation_id=None, user_id=None, kb_id=None, question=""):
        self.trace_id = trace_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.kb_id = kb_id
        self.question = question

        self.started_at = time.time()
        self.route = None
        self.error = None
        self.steps: list[dict] = []          # 采集到的节点执行记录
        self._step_runs: dict = {}           # run_id -> 节点执行上下文
        self.llm_call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        # 【v1.16】本次链路是否出现过「输出被 max_tokens 截断」；
        # 在观测层独立采集，业务节点即使漏判也能被发现。
        self.is_truncated = False

    # ---------- 节点执行 ----------

    def on_chain_start(self, serialized, inputs, **kwargs):
        try:
            name = (serialized or {}).get("name")
            if name not in _STEP_MAP:
                meta = kwargs.get("metadata") or {}
                name = meta.get("langgraph_node") or meta.get("langgraph_step")
            if name in _STEP_MAP:
                self._step_runs[kwargs.get("run_id")] = {
                    "step_name": _STEP_MAP[name][0],
                    "tool_name": _STEP_MAP[name][1],
                    "start": time.time(),
                }
                # 写入当前工具执行 trace 上下文，供重试包装器记录失败明细
                tool_trace_context_var.set({
                    "trace_id": self.trace_id,
                    "step_name": _STEP_MAP[name][0],
                    "tool_name": _STEP_MAP[name][1],
                })
        except Exception:
            logger.debug("trace on_chain_start 处理失败", exc_info=True)

    def on_chain_end(self, outputs, **kwargs):
        try:
            rec = self._step_runs.pop(kwargs.get("run_id"), None)
            if not rec:
                return
            latency_ms = int((time.time() - rec["start"]) * 1000)
            if rec["step_name"] == "router":
                # 捕获路由分类结果
                if isinstance(outputs, dict):
                    self.route = outputs.get("route", self.route)
                return
            tool_output = ""
            if isinstance(outputs, dict):
                tool_output = outputs.get("tool_output") or outputs.get("final_answer") or ""
            self.steps.append({
                "step_name": rec["step_name"],
                "tool_name": rec["tool_name"],
                "tool_input": {"question": self.question, "kb_id": self.kb_id},
                "tool_output": (tool_output or "")[:8000],
                "latency_ms": latency_ms,
                "status": "success",
                "error_msg": None,
                "start": rec["start"],
            })
        except Exception:
            logger.debug("trace on_chain_end 处理失败", exc_info=True)

    def on_chain_error(self, error, **kwargs):
        try:
            self.error = str(error)[:1000]
            rec = self._step_runs.pop(kwargs.get("run_id"), None)
            if rec and rec["step_name"] != "router":
                self.steps.append({
                    "step_name": rec["step_name"],
                    "tool_name": rec["tool_name"],
                    "tool_input": {"question": self.question, "kb_id": self.kb_id},
                    "tool_output": None,
                    "latency_ms": int((time.time() - rec["start"]) * 1000),
                    "status": "failed",
                    "error_msg": self.error,
                    "start": rec["start"],
                })
        except Exception:
            logger.debug("trace on_chain_error 处理失败", exc_info=True)

    # ---------- LLM token ----------

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.llm_call_count += 1

    def on_llm_end(self, response, **kwargs):
        try:
            usage = _extract_usage(response)
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            self.total_tokens += usage["total_tokens"]
            # 【v1.16】观测截断：任意一次调用命中 finish_reason=length / stop_reason=max_tokens
            # 都置位。这里只记录不干预——是否给用户追加提示由 answer 节点决定。
            if is_truncated_result(response):
                self.is_truncated = True
                logger.warning(
                    "LLM 输出被截断 trace_id=%s output_tokens=%s（检查 LLM_MAX_TOKENS）",
                    self.trace_id, usage["output_tokens"],
                )
        except Exception:
            logger.debug("trace 提取 token 失败", exc_info=True)

    # ---------- 落库 ----------

    def flush(self, status: str = "success") -> None:
        """把采集到的数据写入 trace_logs / trace_tool_calls；任何异常仅记录日志。"""
        try:
            ensure_trace_tables()
            now = datetime.now()
            total_latency_ms = int((time.time() - self.started_at) * 1000)

            db = SessionLocal()
            try:
                db.add(TraceLog(
                    trace_id=self.trace_id,
                    conversation_id=self.conversation_id,
                    message_id=None,
                    user_id=self.user_id,
                    kb_id=self.kb_id,
                    agent_route=self.route,
                    # 【v1.18】原先只读 settings.llm_model（.env 快照），而配置页改的是
                    # runtime_config 覆盖值 —— 于是「实际用 qwen3.7-plus 跑、Trace 全记成
                    # deepseek-chat」。观测数据必须与真实调用同源，否则按模型做成本/质量
                    # 归因全是错的。取值顺序与工厂一致：配置页覆盖 > .env > 兜底。
                    model=str(
                        runtime_config.get_value("llm_model")
                        or settings.llm_model
                        or "deepseek-chat"
                    ),
                    input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens,
                    total_tokens=self.total_tokens,
                    is_truncated=bool(self.is_truncated),
                    llm_call_count=self.llm_call_count,
                    tool_call_count=len(self.steps),
                    total_latency_ms=total_latency_ms,
                    status=status,
                    error_msg=self.error,
                    started_at=datetime.fromtimestamp(self.started_at),
                    ended_at=now,
                ))
                for s in self.steps:
                    db.add(TraceToolCall(
                        trace_id=self.trace_id,
                        step_name=s["step_name"],
                        tool_name=s["tool_name"],
                        tool_input=s["tool_input"],
                        tool_output=s["tool_output"],
                        latency_ms=s["latency_ms"],
                        status=s["status"],
                        error_msg=s["error_msg"],
                        started_at=datetime.fromtimestamp(s["start"]),
                        ended_at=now,
                    ))
                db.commit()
            finally:
                db.close()
        except Exception:
            # 约束：日志写入失败不中断主流程，仅打印 error
            logger.error("Trace 日志写入失败 trace_id=%s", self.trace_id, exc_info=True)
