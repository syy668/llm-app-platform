"""应用配置加载模块。

统一从项目根目录 .env 读取配置（数据库、Redis、密钥等），
禁止在代码中硬编码任何地址、账号、密码。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = 本文件(app/config.py)的上两级目录；使用 pathlib 保证 Windows/Linux 兼容。
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载项目根目录下的 .env（不存在时静默跳过，由下方 _require 兜底报错）。
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    """读取必需的环境变量，缺失或为空时抛出明确错误。"""
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(
            f"缺少环境变量 {name}，请在项目根目录 .env 中配置（可参考 .env.example）"
        )
    return value


def _int(name: str, default: int) -> int:
    """读取整型环境变量，解析失败时回退默认值。"""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    """全局配置单例，所有字段均从环境变量读取。"""

    def __init__(self) -> None:
        # 数据库连接串，如 mysql+pymysql://user:pass@127.0.0.1:3306/kb_agent?charset=utf8mb4
        self.database_url: str = _require("DATABASE_URL")
        # Redis 连接串，如 redis://:pass@127.0.0.1:6379/1
        # 末尾 /N 为逻辑库编号：本项目默认独占 /1，避免与同机其它服务共用 /0 互相影响
        self.redis_url: str = _require("REDIS_URL")
        # 用于签名 session token 的密钥
        self.secret_key: str = _require("SECRET_KEY")

        # session / refresh token 有效期（秒）
        # 【双Token改造】access（session）改为短期 30min，配合 refresh_token 无感刷新；
        # refresh 保持 7 天。已显式配置 SESSION_TTL_SECONDS 的环境不受影响。
        self.session_ttl: int = _int("SESSION_TTL_SECONDS", 1800)
        self.refresh_ttl: int = _int("REFRESH_TTL_SECONDS", 604800)

        # 【A3 绝对生命周期】一次登录（含任意次续期/刷新）的最长存活时间，默认 30 天。
        # 与 session_ttl / refresh_ttl 是**两个维度**：后两者是"多久不用就过期"（滑动），
        # 本项是"无论多活跃，到点必须重新登录"（绝对）。缺了它，持续访问即可永久驻留。
        self.token_max_lifetime: int = _int("TOKEN_MAX_LIFETIME_SECONDS", 30 * 24 * 3600)

        # 启动时是否自动建表（默认开启：create_all 幂等只建缺失表，v1.5 升级时
        # 自动补齐新增表，避免旧库缺表导致 500；已有表结构永不改动）
        self.auto_create_tables: bool = os.getenv("AUTO_CREATE_TABLES", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        # 【v1.7 新增】启动期 ORM / 数据库结构一致性自检模式：
        #   strict（默认）= 缺列/缺表时打 ERROR 日志并中止启动（fail fast，避免半路 500）；
        #   warn          = 只告警继续启动（适合"先起服务再补库"的运维场景）；
        #   off           = 关闭检查。
        # create_all 不会给存量表补列，故只有自检能提前发现"ORM 有、库没有"的字段。
        _schema_mode = os.getenv("SCHEMA_CHECK_MODE", "strict").strip().lower() or "strict"
        self.schema_check_mode: str = (
            _schema_mode if _schema_mode in ("strict", "warn", "off") else "strict"
        )

        # 跨域来源，逗号分隔，* 表示全部
        self.cors_origins: list[str] = [
            o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
        ]

        self.app_name: str = os.getenv("APP_NAME", "企业垂直知识库 Agent 后台")
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")

        # 【工程化收尾 新增】运行环境：dev=开发（异常返回完整堆栈）/ prod=生产（只返回简洁提示）
        # 默认 prod：未显式配置时按最安全策略处理，本地开发请在 .env 中设 APP_ENV=dev
        self.env: str = os.getenv("APP_ENV", "prod").lower()
        self.debug: bool = self.env == "dev"

        # ---------- RAG / 向量库 / LLM ----------
        # 上传文件存储目录（相对项目根目录）
        self.upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")
        # 单文件上传大小上限（MB），超过则上传接口直接拒绝
        self.max_file_size_mb: int = _int("MAX_FILE_SIZE_MB", 20)
        # Chroma 向量库持久化目录（相对项目根目录）
        self.chroma_persist_dir: str = os.getenv("CHROMA_PERSIST_DIR", "chroma_db")

        # Embedding 提供方：hash=本地特征哈希(免密钥，测试用) / openai=OpenAI 兼容接口
        self.embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "hash").lower()
        self.embedding_api_base: str = os.getenv("EMBEDDING_API_BASE", "")
        self.embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "")
        self.embedding_dim: int = _int("EMBEDDING_DIM", 384)

        # LLM 提供方：mock=回显检索结果(免密钥，测试用) / openai=OpenAI 兼容 / anthropic=Claude
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "mock").lower()
        self.llm_api_base: str = os.getenv("LLM_API_BASE", "")
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "")
        self.llm_model: str = os.getenv("LLM_MODEL", "")

        # ---- 【v1.16】输出端硬上限与历史窗口护栏 ----
        # 单次回答的最大输出 token（默认 2048，约 1300~2000 汉字）。
        # 为什么必须显式设：不传时由上游默认值决定，而各家默认值差异极大
        # （ChatAnthropic 默认仅 1024，比本系统常见回答长度还短，会静默截断）；
        # 同时对成本封顶——单条回答不可能烧掉 8k token。
        self.llm_max_tokens: int = max(64, min(32768, _int("LLM_MAX_TOKENS", 2048)))
        # 送入上下文的历史轮数上限（1 轮 = 1 问 + 1 答，默认 5）。
        # 此前全量拉取 .all()，长会话的 prompt 随轮数线性膨胀，成本与延迟双失控。
        self.chat_history_max_turns: int = max(1, min(50, _int("CHAT_HISTORY_MAX_TURNS", 5)))
        # 历史字符总预算（默认 2000 字）：即使 5 轮内，遇到长文档粘贴也要能截住。
        # 超预算时从**最早**的消息开始丢弃，保留最近的上下文。
        self.chat_history_max_chars: int = max(200, min(50000, _int("CHAT_HISTORY_MAX_CHARS", 2000)))

        # ---- 评测性能（并发/重试/批量落库）【新增】----
        # 并发评测开关：false 时回退原串行逐条执行（调试用），结果口径与串行完全一致
        self.eval_concurrent_enable: bool = os.getenv("EVAL_CONCURRENT_ENABLE", "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # 并发上限（1~10）：防止打满 DeepSeek / 硅基流动触发 429 限流；默认 2
        self.eval_max_concurrency: int = max(1, min(10, _int("EVAL_MAX_CONCURRENCY", 2)))
        # 单样本失败自动重试次数（含首次；1=不重试），上限 5
        self.eval_retry_attempts: int = max(1, min(5, _int("EVAL_RETRY_ATTEMPTS", 2)))
        # 得分批量落库阈值：内存暂存 N 条后一次性写入（减少频繁 commit）。
        # 【v1.13.1】默认值由 10 改为 1（= 每样本立即落库）。
        # 原默认 10 会留下「最多 9 条已算完的得分在内存里」的丢分窗口：任务异常、
        # 进程被 kill、用户点停止，缓冲区尾部直接蒸发。真实库里已实测到
        # scores 恒为 10 的整数倍且永远少于 success_items（run 33：9/30 成功、0 条得分）。
        # 一个样本的评测要调 4 次 RAGAS + 1 次问答，多一次 commit 的相对成本可忽略，
        # 而丢掉的样本要重新花钱跑。批量阈值保留，供极端降频场景（如 sqlite 本地联调）显式调大。
        self.eval_score_batch_size: int = max(1, min(100, _int("EVAL_SCORE_BATCH_SIZE", 1)))
        # 【新增】同时处于 pending/running 的评测任务数上限（批量执行的并发闸）。
        # 为什么需要：批量触发 M 个任务 = M 个后台线程，每个线程内部还有
        # eval_max_concurrency 个 worker，实际并发 = M × N 路打同一个上游 key，
        # 不设闸就会把 429 从"偶发"变成"必然"。默认 3（默认并发 2 → 最多 6 路）。
        self.eval_max_active_runs: int = max(1, min(20, _int("EVAL_MAX_ACTIVE_RUNS", 3)))

        # ---- 评测稳定性（本轮增量）【新增】----
        # RAGAS evaluate() 整体超时（秒）：这是「单样本四项指标」的任务级上限，
        # 必须大于 单请求超时 × (重试+1)，否则会先于 LLM 超时被打断（项目二同款公式）。
        # 240s × (3+1) = 960 → 默认给 1200 留余量（EVAL_RAGAS_TIMEOUT 可覆盖）
        self.eval_ragas_timeout: int = max(60, _int("EVAL_RAGAS_TIMEOUT", 1200))
        # RAGAS Judge LLM 单请求超时（秒）：长答案的 NLI 阶段（几十条陈述逐一判定）
        # 实测 40~110s，120s 会把长样本拖成超时空值（run72 的 9/30 条空值即此因）
        self.eval_llm_timeout: int = max(30, _int("EVAL_LLM_TIMEOUT", 240))
        # 【v1.19.x】判分模型输出上限（与对话 max_tokens 解耦）：faithfulness 的 NLI 输出
        # 是「每条陈述 = 原文+理由+verdict」的巨型 JSON，长中文答案可拆出几十条陈述，
        # 2048 会被截断 → JSON 解析失败 → 指标 NaN。判分专用 8192，不影响对话模型。
        self.eval_llm_max_tokens: int = max(256, min(32768, _int("EVAL_LLM_MAX_TOKENS", 8192)))
        # RAGAS Judge LLM 单请求最大重试次数（0~10，默认 3）
        self.eval_llm_max_retries: int = max(0, min(10, _int("EVAL_LLM_MAX_RETRIES", 3)))
        # 僵死任务阈值（秒）：应用启动时，started_at 早于该阈值仍为 running 的评测任务判为中断
        self.eval_stale_run_seconds: int = max(60, _int("EVAL_STALE_RUN_SECONDS", 600))
        # 【v1.17】开跑前判分模型探活：一次最小调用验证判分链路可用，
        # 不可用则任务直接 failed 并写明原因，而不是让每个样本白跑一遍检索+生成
        # 再得到「指标全空」的假成功（真实故障 run 63/64/65）。
        # 单测默认关闭（不得向真实上游发请求），由 conftest 统一 monkeypatch。
        self.eval_preflight_enable: bool = os.getenv(
            "EVAL_PREFLIGHT_ENABLE", "true"
        ).strip().lower() in ("1", "true", "yes", "on")


settings = Settings()
