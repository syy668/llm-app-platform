"""RAGAS 评测接口。

权限说明（本次增量调整）：
  - GET 查询类接口（datasets / runs 及明细）：登录用户可读（get_current_user）；
  - POST / DELETE 写操作：仅管理员（require_admin）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import CurrentUser, get_current_user, require_admin
from ...core.database import get_db
from ...core.openapi import std_errors
from ...core.response import ok
from ...schemas.eval import (
    DatasetCreate,
    ItemsImport,
    ItemUpdate,
    RunCompareRequest,
    RunCreate,
)
from ...services import eval_service

router = APIRouter(prefix="/eval", tags=["RAGAS 评测"])


@router.post("/datasets")
def create_dataset(
    payload: DatasetCreate,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """创建评测数据集。"""
    return ok(eval_service.create_dataset(db, operator, payload))


@router.get("/datasets")
def list_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    kb_id: int | None = None,
    _: CurrentUser = Depends(get_current_user),  # 【新增】由 require_admin 放开为登录可读
    db: Session = Depends(get_db),
):
    """查询评测数据集。"""
    return ok(eval_service.list_datasets(db, page, size, kb_id))


@router.post("/datasets/{dataset_id}/items")
def add_items(
    dataset_id: int,
    payload: ItemsImport,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量导入评测样本。"""
    return ok(eval_service.add_items(db, dataset_id, payload))


# 【v1.13 新增】样本修正：黄金集参考答案补全 / 脏样本清理
# 为什么必须有：点踩自动沉淀的样本 reference_answer 为空串（防止把 AI 的错误
# 答案当标准答案），而旧版只有「追加」没有「修改」，这批样本的 context_recall
# 永远算不出来 —— 前端「可在评测页补充参考答案」的提示因此无法兑现。
@router.put("/datasets/{dataset_id}/items/{item_id}")
def update_item(
    dataset_id: int,
    item_id: int,
    payload: ItemUpdate,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """修改单条样本（PATCH 语义：只更新请求体里出现的字段）。

    - `reference_answer` 出现时必须非空，空串/纯空白返回 400（留空样本无法评测）；
    - 只传 reference_answer 时不会清空 stratum / reference_contexts；
    - 参考答案变更写 operation_logs（含新旧值前 500 字），便于追溯黄金集被谁改过。

    入参示例：PUT /api/v1/eval/datasets/3/items/57  {"reference_answer": "差旅住宿上限 500 元/晚"}
    返回示例：{"code": 0, "message": "ok", "data": {"id": 57, "reference_answer": "差旅住宿上限 500 元/晚", ...}}
    错误码：400 参考答案为空 / 无可更新字段；401 未登录；403 非管理员；404 数据集或样本不存在（含跨集 item_id）。
    """
    return ok(eval_service.update_item(db, dataset_id, item_id, payload, operator))


@router.delete("/datasets/{dataset_id}/items/{item_id}")
def delete_item(
    dataset_id: int,
    item_id: int,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """删除单条样本（并重算所属数据集的 item_count）。

    错误码：401 未登录；403 非管理员；404 数据集或样本不存在（含跨集 item_id）。
    """
    return ok(eval_service.delete_item(db, dataset_id, item_id, operator))


@router.get("/datasets/{dataset_id}/items")
def list_items(
    dataset_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: CurrentUser = Depends(get_current_user),  # 【新增】由 require_admin 放开为登录可读
    db: Session = Depends(get_db),
):
    """查询评测样本列表。"""
    return ok(eval_service.list_items(db, dataset_id, page, size))


@router.delete("/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: int,
    _: CurrentUser = Depends(require_admin),  # 【新增】删除数据集（级联删样本，保留运行记录）
    db: Session = Depends(get_db),
):
    """删除评测数据集（级联删除样本；eval_runs 运行记录保留）。"""
    return ok(eval_service.delete_dataset(db, dataset_id))


@router.post("/runs")
def create_run(
    payload: RunCreate,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """触发异步后台评测任务（立即返回，状态 pending → running → done/failed）。

    【新增】payload.rag_config 可选：请求级 RAG 实验参数，随任务持久化并透传问答链路。
    """
    return ok(eval_service.create_run(db, operator, payload))


@router.get("/runs")
def list_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    _: CurrentUser = Depends(get_current_user),  # 【新增】由 require_admin 放开为登录可读
    db: Session = Depends(get_db),
):
    """查询评测运行记录。"""
    return ok(eval_service.list_runs(db, page, size, status))


# 【v1.13.2 新增】大盘聚合统计。
# 必须定义在 /runs/{run_id} **之前**：FastAPI 按声明顺序匹配，若在后面，
# 请求 /runs/stats 会先命中 /runs/{run_id} 这一条，然后 int 转换 "stats" 失败 → 422。
@router.get("/runs/stats", responses=std_errors(401, 500))
def get_run_stats(
    _: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """评测大盘统计：任务计数 + 四项指标均值（聚合口径见 scope 字段）。

    - 计数覆盖全表所有状态（total/pending/running/done/failed/cancelled）；
    - 指标**只统计 done**，并按 success_items 加权（理由：failed/cancelled 样本数不齐、
      冒烟任务不该与黄金集任务等权）；
    - 四项指标各自独立分母，某指标全部为空时返回 null（前端显示「—」，绝不显示 0）；
    - 不查 eval_run_scores，只做两次轻量聚合，任务量增长不影响接口耗时量级。

    错误码：401 未登录；500 服务端异常。
    """
    return ok(eval_service.run_stats(db))


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    _: CurrentUser = Depends(get_current_user),  # 【新增】由 require_admin 放开为登录可读
    db: Session = Depends(get_db),
):
    """评测运行详情（含汇总 metrics 与 rag_config）。"""
    return ok(eval_service.get_run(db, run_id))


@router.post("/runs/compare", responses=std_errors(400, 401, 422, 500))
def compare_runs(
    payload: RunCompareRequest,
    _: CurrentUser = Depends(get_current_user),  # 纯查询不落库：登录可读
    db: Session = Depends(get_db),
):
    """【B1-5 新增】A/B 两两对比聚合接口。

    - 入参：run_id_a / run_id_b（均为已有评测运行 ID，必填且 >=1）；
    - 前置校验：两个 run 必须属于同一 dataset_id，否则 400；
    - 后端完成样本 question 对齐、四项指标差值 Δ 与汇总计算，
      前端只负责渲染，不再自行拉全量分数计算；
    - 返回：run_a / run_b 详情 + summary（四项指标均值与差值）+ rows（逐样本对比，
      含 diffs 与 is_diff 标记，|Δ|>0.1 判定为差异样本，阈值随响应返回）。

    返回示例：{"code": 0, "message": "ok", "data": {
        "run_a": {...}, "run_b": {...},
        "summary": [{"metric": "faithfulness", "avg_a": 0.5, "avg_b": 0.7, "diff": -0.2}, ...],
        "rows": [{"question": "q1", "a": {...}, "b": {...}, "diffs": {...}, "is_diff": true}, ...],
        "diff_threshold": 0.1}}
    错误码：400 两个 run 不属于同一数据集；401 未登录；422 参数不合法；500 服务端异常。
    """
    return ok(eval_service.compare_runs(db, payload.run_id_a, payload.run_id_b))


@router.get("/runs/{run_id}/scores")
def list_scores(
    run_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: CurrentUser = Depends(get_current_user),  # 【新增】由 require_admin 放开为登录可读
    db: Session = Depends(get_db),
):
    """单条评测得分详情。"""
    return ok(eval_service.list_scores(db, run_id, page, size))


@router.get("/runs/{run_id}/badcases", responses=std_errors(401, 422, 500))
def list_badcases(
    run_id: int,
    # 【B1-4 新增】BadCase 后端筛选：threshold=低分阈值，metric=参与筛选的指标
    metric: str = Query(
        "faithfulness",
        pattern="^(faithfulness|answer_relevancy|context_precision|context_recall)$",
        description="参与筛选的指标名（白名单）",
    ),
    threshold: float = Query(
        0.6, ge=0.0, le=1.0, description="低分阈值：metric < threshold 判为 BadCase"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: CurrentUser = Depends(get_current_user),  # 查询接口：登录可读
    db: Session = Depends(get_db),
):
    """BadCase 分页查询：指定指标低于阈值的样本（后端筛选，非前端计算）。

    - 筛选口径：metric 非空 且 metric < threshold，NULL 指标不参与（统一空值口径）；
    - 每条含 question / ground_truth / answer / retrieved_contexts / 四项指标分数；
    - ground_truth 来自评测数据集 reference_answer（LEFT JOIN 补齐，缺失为 null）；
    - 按指标值升序排列（最差样本排最前）。

    入参示例：GET /api/v1/eval/runs/1/badcases?metric=faithfulness&threshold=0.6&page=1&page_size=20
    返回示例：{"code": 0, "message": "ok", "data": {"total": 3, "page": 1, "page_size": 20,
               "items": [{"question": "...", "score": {"faithfulness": 0.32, ...}}, ...]}}
    错误码：401 未登录；422 metric 不在白名单 / 阈值越界 / 分页参数非法；500 服务端异常。
    """
    return ok(eval_service.list_badcases(db, run_id, metric, threshold, page, page_size))


@router.get("/runs/{run_id}/stratum_metrics", responses=std_errors(401, 404, 500))
def get_stratum_metrics(
    run_id: int,
    _: CurrentUser = Depends(get_current_user),  # 查询接口：登录可读
    db: Session = Depends(get_db),
):
    """【B2-4 新增】按 stratum 分层聚合四项 RAGAS 指标。

    - 数据来源：eval_run_scores 按 item_id LEFT JOIN eval_dataset_items 取 stratum 分组；
    - stratum 为 null 表示"未分层"（含历史数据与数据集样本已删除两种情况）；
    - 聚合口径：SQL AVG 忽略 NULL，与全站统一空值规则一致；
    - 返回：{run_id, total, items:[{stratum, sample_count, metrics:{四项均值}}]}。

    入参示例：GET /api/v1/eval/runs/1/stratum_metrics
    返回示例：{"code": 0, "message": "ok", "data": {"run_id": 1, "total": 12,
               "items": [{"stratum": "难例", "sample_count": 5, "metrics": {"faithfulness": 0.61, ...}}]}}
    错误码：401 未登录；404 运行不存在；500 服务端异常。
    """
    return ok(eval_service.get_stratum_metrics(db, run_id))


@router.post("/runs/{run_id}/cancel", responses=std_errors(400, 401, 404, 500))
def cancel_run(
    run_id: int,
    operator: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """停止评测任务（v1.13 新增，协作式取消，接口立即返回）。

    - 立即把状态置为 cancelled，后台线程在**下一个样本检查点**退出；
      当前正在跑的那一条会被跑完（同步阻塞的 LLM 调用没有安全中断手段），
      但结果不浪费 —— 检查点放在样本回收之后，已完成的得分照常落库；
    - cancelled 的 run 不会被 A/B 对比选中（前端只放行 done），因为样本数不齐，
      均值与完整运行不可比；
    - 幂等：已是 cancelled 时重复调用返回当前状态；done/failed 报 400。

    错误码：400 任务已结束（done/failed）；401 未登录；403 非管理员；404 任务不存在。
    """
    return ok(eval_service.cancel_run(db, run_id, operator))


@router.post("/runs/cleanup-stale", responses=std_errors(401, 403, 500))
def cleanup_stale_runs(
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """手动清理僵尸评测任务（v1.13 新增，管理员）。

    什么时候需要：服务异常退出后，残留的 running 任务已没有执行线程，却永久占着
    「同时运行评测任务数」的名额，导致新建任务一直 429 而界面上看不到在跑的任务。
    启动时已自动清理一次；本接口给的是运行期的显式出口 —— 刻意不放在 create_run
    里自动做，因为那会在创建任务的路径上偷偷改别的任务状态。

    阈值 EVAL_STALE_RUN_SECONDS（默认 600s）。注意：正在正常执行且已超过该时长的
    任务会被误判，调用前请确认界面上确实没有在跑的任务。

    【v1.13.1】同时回填历史脏数据的汇总指标：v1.13.1 之前中断的任务只落了得分、
    没落 metrics，列表里四项指标全是「—」而点开能看到明细。返回 repaired 供前端提示。
    """
    cleared = eval_service.cleanup_stale_running_runs()
    repaired = eval_service.backfill_terminal_metrics()
    return ok({"cleared": cleared, "repaired": repaired})


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: int,
    _: CurrentUser = Depends(require_admin),  # 【新增】删除评测任务（级联删得分明细）
    db: Session = Depends(get_db),
):
    """删除评测运行记录（级联删除得分明细）。"""
    return ok(eval_service.delete_run(db, run_id))
