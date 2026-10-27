"""全局配置。

优先级：环境变量 > .env > 默认值（pydantic-settings 默认行为）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务自身 ----------
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8200
    API_PREFIX: str = "/api/v1"
    # 兼容挂载：/eval/* 与 /api/v1/eval/* 同时可用（前端与项目三两套调用习惯）
    COMPAT_PREFIX: str = ""
    DEBUG: bool = True

    # ---------- 数据库 ----------
    DATABASE_URL: str = (
        "mysql+pymysql://root:12345678@127.0.0.1:3306/eval_agent_db?charset=utf8mb4"
    )
    SQL_ECHO: bool = False
    # 启动时自动 create_all（只补表不补列；生产改 False + alembic）
    AUTO_CREATE_TABLES: bool = True
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ---------- 跨域 ----------
    # 逗号分隔；"*" 表示全放行（前端 5174 / 项目一 5173 / 项目三 5173 各自跨域直连）
    CORS_ORIGINS: str = "*"

    # ---------- 出网 ----------
    # 是否让 httpx / openai SDK 读取系统代理（HTTP_PROXY / HTTPS_PROXY / NO_PROXY）。
    # 默认 **False（直连）**：本中台只跟两类地址说话 ——
    #   ① 本地被测系统 127.0.0.1:8010（走代理毫无意义，还会被代理的空闲/连接数限制打断）
    #   ② 国内公有模型 API（dashscope / siliconflow，本身直连可达）
    # 实测教训（2026-10-27）：本机一旦存在代理环境变量，httpx 默认会**连 127.0.0.1 也塞进代理**；
    # 代理抖动时表现就是「打分超时 >300s」+「APIConnectionError」，看起来像样本算不动、
    # 模型不支持结构化输出 —— 实际是链路被劫持。需要走代理的机器把它设成 true。
    HTTP_TRUST_ENV: bool = False

    # ---------- 上游：项目一 RAG 问答（被测系统）----------
    # 注意：8000 端口实测跑的是项目三（AgentFlow），项目一在 8010。
    # 换机器/换部署时只改这一行即可。
    UPSTREAM_RAG_BASE_URL: str = "http://127.0.0.1:8010"
    UPSTREAM_RAG_TIMEOUT: float = 60.0
    UPSTREAM_RAG_CONNECT_TIMEOUT: float = 5.0
    UPSTREAM_RAG_CONCURRENCY: int = 4
    # 鉴权二选一：静态 token 优先；为空则用账号密码自动登录并缓存
    UPSTREAM_RAG_TOKEN: str = ""
    UPSTREAM_RAG_USERNAME: str = ""
    UPSTREAM_RAG_PASSWORD: str = ""
    # 项目一 access_token 有效期 30 分钟，提前 60s 刷新
    UPSTREAM_RAG_TOKEN_TTL: int = 1740

    # ---------- 鉴权（身份仍归项目一，项目二只做校验，不建用户表）----------
    # 原则：「身份是上游资产，配置是本中台资产」。写配置这类管理操作，
    # 用上游 token 调 GET /auth/me 换出 role 再判 admin，避免项目二自建一套账号体系。
    AUTH_ENFORCE: bool = True
    # 留空则复用 UPSTREAM_RAG_BASE_URL（本机联调同为 8010）
    AUTH_UPSTREAM_BASE_URL: str = ""
    AUTH_TIMEOUT: float = 8.0
    # token → 用户信息缓存秒数（避免每次写操作都打上游一次）
    AUTH_CACHE_TTL: int = 60

    # ---------- 裁判模型（RAGAS 打分，跑在本仓库）----------
    JUDGE_PROVIDER: str = "openai"
    JUDGE_MODEL: str = ""
    JUDGE_API_KEY: str = ""
    JUDGE_API_BASE: str = ""
    # answer_relevancy 需要向量；没有它这一项必然失败
    JUDGE_EMBEDDING_MODEL: str = "text-embedding-v3"
    # 单次 LLM 调用超时（openai SDK 层）。
    # 注意：ragas 的 LangchainLLMWrapper 会把 RunConfig.timeout（= JUDGE_METRIC_TIMEOUT）
    # 覆盖到 langchain_llm.request_timeout 上，所以真正生效的是下面那个值；
    # 这里保持同量级，避免「两处配置说的是两件事」导致排障时被误导。
    JUDGE_TIMEOUT: float = 120.0
    JUDGE_MAX_CONCURRENCY: int = 3
    # 4096 对 faithfulness 的 NLI 输出偏紧：7 句答案能拆出 20 条陈述，
    # 每条陈述都要一条 verdict JSON，长答案（37 句）会直接顶到上限被截断，
    # 截断 → 解析失败 → 指标 NaN（这是「答案明明有内容却空」的一条隐性路径）。
    JUDGE_MAX_TOKENS: int = 8192
    # RAGAS 的 RunConfig.timeout —— 它同时是「单次 HTTP 请求超时」。
    # 实测（2026-10-27，deepseek-flash，KB6 样本）：
    #   seq1（330 字 / 7 句）→ 陈述抽取 13.7s + 20 条陈述 NLI 41.4s ≈ 55s；
    #   seq3（1235 字 / 37 句）在 200s 内跑不完；
    #   seq2 四个指标并发时整条样本 > 300s。
    # 旧值 90s 低于上面任一项 → faithfulness/CP 被硬超时吞成 NaN（实测 4/7 空）。
    # 现在 240s 覆盖长答案单次 NLI；对应把样本硬超时抬到 420s（见 JUDGE_ITEM_TIMEOUT）。
    JUDGE_METRIC_TIMEOUT: int = 240
    JUDGE_MAX_RETRIES: int = 3
    JUDGE_MAX_WAIT: int = 10
    # 外层硬超时：单样本超过该秒数直接判失败，不阻塞其余样本。
    # 约束：JUDGE_ITEM_TIMEOUT > JUDGE_METRIC_TIMEOUT（4 指标并行，取最长的那一路）
    JUDGE_ITEM_TIMEOUT: float = 420.0
    JUDGE_LOG_TENACITY: bool = False
    # 单条样本里个别指标返回 NaN 时，对这一两个指标定向补跑一次
    # （ragas 的 raise_exceptions=False 会把异常吞成 NaN，四项里坏一项时不该整条丢弃）
    JUDGE_MISSING_METRIC_RETRY: bool = True
    # faithfulness 的切句器：True = 用内置的中文/Markdown 切句器（默认），
    # False = 沿用 ragas 英文 pysbd（中文 Markdown 答案会切出 0 句 → 指标恒 NaN）
    JUDGE_CN_SEGMENTER: bool = True
    # 打分超时的样本在 run 收尾前补跑一次（实测这些样本单独跑 20~40s 就能出满分，
    # 超时几乎都是瞬时网络抖动 + 客户端重试叠加，不是样本本身难）
    RUN_RESCUE_TIMEOUTS: bool = True
    # 补跑总预算：超出即放弃剩余样本，避免一条烂网把整个 run 拖成小时级
    RUN_RESCUE_BUDGET_SECONDS: float = 600.0
    # dashscope 上的思考型模型必须显式关思考，否则 content 为空、RAGAS 静默 NaN
    JUDGE_DISABLE_THINKING: bool = True
    # 计费单价（元/百万 token），0 表示不折算成本
    JUDGE_PRICE_PER_1M: float = 0.0

    # ---------- 评测执行 ----------
    RUN_ITEM_CONCURRENCY: int = 3
    # 启动时把超过该时长仍 running 的任务判为僵死（进程被杀会留下永久 running）
    RUN_STALE_SECONDS: int = 3600
    # 取消标记轮询间隔：执行器在等打分结果时的最长「无响应窗口」。
    # 不能用「等一条样本算完再看标记」——单条最长 JUDGE_ITEM_TIMEOUT=300s，
    # 用户点了停止却要等 5 分钟才生效，实操上等于没停。
    RUN_CANCEL_POLL_SECONDS: float = 2.0
    # 连续 N 条样本「四项指标全空」即判定裁判模型不兼容，提前终止整轮。
    # 取 3：单条偶然解析失败（输出被截断）不该判死整轮，但连续三条空必然不是偶然。
    RUN_EMPTY_METRIC_STREAK_LIMIT: int = 3
    # 拒答样本（标准答案是「知识库中未找到相关信息」这类）不参与 answer_relevancy 聚合：
    # AR 是「由答案反推问题、再和原问题比相似度」，拒答话术没有主题内容，
    # 该指标恒为 0 且与答对答错无关 —— 不排除会系统性低估整体 AR（实测 4 条拒答把 0.924 压到 0.822）。
    REFUSAL_SKIP_AR: bool = True
    # 单指标缺失率告警阈值：某个指标在成功样本里的空缺比例超过该值，
    # 就在 metric_detail.warnings 里点名（均值仍算，但结论只能当参考）
    METRIC_MISSING_WARN_RATE: float = 0.2

    # ---------- 评测集 ----------
    # 单个评测集**总量**上限（不是单次导入上限 —— 那个由 ItemsImport.max_length=500 管）。
    # 支持"追加样本"之后必须有这道闸：否则反复追加可以无限撑大，
    # 前端页面写的"单数据集最多 500 条"就成了假承诺，而且一轮评测会无上限地烧裁判额度。
    DATASET_MAX_ITEMS: int = 500
    # BadCase / A/B 的判定阈值
    BADCASE_DEFAULT_THRESHOLD: float = 0.6
    COMPARE_DIFF_THRESHOLD: float = 0.1

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
