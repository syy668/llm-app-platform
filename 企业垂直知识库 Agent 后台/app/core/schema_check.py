"""启动期 ORM 模型与数据库结构一致性自检（v1.7 新增）。

## 为什么需要

`create_all(checkfirst=True)` 只建「缺失的表」，**不会给存量表补列**。
所以 ORM 一旦新增列，旧库不会自动升级，问题会潜伏到某个接口真的读写该列时，
才以 `OperationalError: Unknown column 'xxx' in 'field list'` 的形式暴露 ——
对前端而言就是一个没有上下文的 500，而且往往出现在联调/演示最不想出错的时刻。

## 做什么

启动时把 `Base.metadata`（ORM 侧）与实际库结构做一次比对：

- ORM 有、库里没有的表 → 提示执行建表脚本；
- 表存在，但 ORM 有而库里没有的列 → 直接给出可复制执行的 `ALTER TABLE ... ADD COLUMN`。

比对**只判定「存在性」**，不比较类型/默认值/索引差异。原因：各数据库对类型的
归一化差异很大（MySQL 的 `BIGINT(20)` vs `BIGINT`、`TINYINT(1)` vs `BOOLEAN`、
sqlite 的动态类型），强行比较会产生大量误报，反而让人习惯性忽略告警。

## 三种模式（环境变量 SCHEMA_CHECK_MODE）

- `strict`（默认）：发现问题 → 打 ERROR 日志并抛 `SchemaDriftError`，中止启动（fail fast）；
- `warn`：只打 ERROR 日志，服务继续启动（适合「先把服务起来、再补库」的运维场景）；
- `off`：跳过检查。

数据库本身连不上时**不判定为漂移**：只警告并跳过，真实状态由 `/health` 反映。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass

from sqlalchemy import Column, MetaData, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

MODE_STRICT = "strict"
MODE_WARN = "warn"
MODE_OFF = "off"
_VALID_MODES = (MODE_STRICT, MODE_WARN, MODE_OFF)

#: 缺表时的统一处置建议（表结构能从 ORM 直接建出来，不需要手写 DDL）
_MISSING_TABLE_HINT = (
    "执行 scripts/init_db.sql 建表，或设 AUTO_CREATE_TABLES=true 后重启（create_all 只建缺失表）"
)


class SchemaDriftError(RuntimeError):
    """ORM 模型与实际数据库结构不一致（启动期阻断）。

    刻意不继承业务异常：它不是请求期错误，而是部署/升级错误，
    必须在进程启动阶段炸出来，不应该被任何 HTTP 异常处理器接住。
    """


@dataclass(frozen=True)
class DriftIssue:
    """一处结构不一致。"""

    kind: str  # missing_table | missing_column
    table: str
    column: str | None = None
    ddl: str | None = None  # 可直接执行的修复语句（缺表时为 None）
    hint: str = ""  # 补充说明（如「先回填再收紧 NOT NULL」）

    @property
    def label(self) -> str:
        return "缺失列" if self.kind == "missing_column" else "缺失表"

    @property
    def target(self) -> str:
        return f"{self.table}.{self.column}" if self.column else self.table


def import_all_models() -> None:
    """显式导入 `app/models` 下的全部模块，保证 `Base.metadata` 完整。

    必要性：`app/models/__init__.py` 只导入了核心 9 张表的模型，`eval_*` / `trace_*`
    是靠在路由、服务模块里被间接导入才注册的。若自检依赖这种「碰巧已经导入」的状态，
    漏掉的表会被误判成「缺失表」，而这些表内部的列漂移则完全检查不到 ——
    那正是本模块最该抓住的场景。
    """
    from .. import models as models_pkg

    for module in pkgutil.iter_modules(models_pkg.__path__):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"{models_pkg.__name__}.{module.name}")


def _server_default_sql(column: Column) -> str | None:
    """取服务端默认值的 SQL 文本（`server_default` 本身就是原始 SQL 片段）。"""
    default = column.server_default
    if default is None:
        return None
    text = str(getattr(default, "arg", default)).strip()
    return text or None


def _add_column_ddl(
    table_name: str, column: Column, dialect
) -> tuple[str, str | None]:
    """生成 ADD COLUMN 语句；返回 (可直接执行的 DDL, 需要用户收紧时的后续 DDL)。

    关键取舍：ORM 声明 `NOT NULL` 但**没有服务端默认值**时，不能直接生成
    `ADD COLUMN x ... NOT NULL` —— 存量表只要有一行数据，MySQL 严格模式就会
    以 1364/1138 拒绝执行（sqlite 也会报 "Cannot add a NOT NULL column with
    default value NULL"）。与其给出一条跑不通的 DDL，不如先按可空添加、
    再把「回填后收紧」作为第二步给出。
    """
    type_sql = column.type.compile(dialect=dialect)
    default_sql = _server_default_sql(column)

    if not column.nullable and not default_sql:
        ddl = f"ALTER TABLE `{table_name}` ADD COLUMN `{column.name}` {type_sql} NULL;"
        tighten = (
            f"ALTER TABLE `{table_name}` MODIFY COLUMN `{column.name}` {type_sql} NOT NULL;"
        )
        return ddl, tighten

    suffix = "NOT NULL" if not column.nullable else "NULL"
    if default_sql:
        suffix += f" DEFAULT {default_sql}"
    return f"ALTER TABLE `{table_name}` ADD COLUMN `{column.name}` {type_sql} {suffix};", None


def inspect_schema(
    bind: Engine | None = None, metadata: MetaData | None = None
) -> list[DriftIssue]:
    """比对 ORM metadata 与真实库结构，返回全部不一致项（按表名、列序稳定排序）。

    `bind` / `metadata` 可注入，便于测试用临时 sqlite 库验证，不碰生产库。
    """
    if metadata is None:
        import_all_models()
        from .database import Base

        metadata = Base.metadata
    if bind is None:
        from .database import engine

        bind = engine

    inspector = inspect(bind)
    # MySQL 的 lower_case_table_names 因平台而异，表名比较统一转小写
    existing_tables = {name.lower(): name for name in inspector.get_table_names()}

    issues: list[DriftIssue] = []
    for table_name, table in sorted(metadata.tables.items()):
        actual_table = existing_tables.get(table_name.lower())
        if actual_table is None:
            issues.append(
                DriftIssue(
                    kind="missing_table",
                    table=table_name,
                    hint=_MISSING_TABLE_HINT,
                )
            )
            continue

        real_columns = {col["name"].lower() for col in inspector.get_columns(actual_table)}
        for column in table.columns:
            if column.name.lower() in real_columns:
                continue
            ddl, tighten = _add_column_ddl(actual_table, column, bind.dialect)
            hint = f"ORM 声明 NOT NULL，回填存量行后再执行：{tighten}" if tighten else ""
            issues.append(
                DriftIssue(
                    kind="missing_column",
                    table=actual_table,
                    column=column.name,
                    ddl=ddl,
                    hint=hint,
                )
            )
    return issues


def format_drift_report(issues: list[DriftIssue]) -> str:
    """把不一致项渲染成一段「看完就能动手」的中文报告。"""
    columns = sum(1 for i in issues if i.kind == "missing_column")
    tables = sum(1 for i in issues if i.kind == "missing_table")
    lines = [
        f"数据库结构落后于 ORM 模型：{columns} 个列缺失、{tables} 张表缺失（共 {len(issues)} 处）。",
        "ORM 新增的字段不会自动补到存量表（create_all 只建缺失的表），继续运行会在读写这些字段时返回 500。",
        "",
    ]
    for issue in issues:
        lines.append(f"  [{issue.label}] {issue.target}")
        if issue.ddl:
            lines.append(f"      修复：{issue.ddl}")
        if issue.hint:
            lines.append(f"      说明：{issue.hint}")
    lines.extend(
        [
            "",
            "执行上述 DDL 后重启服务即可。临时跳过阻断：SCHEMA_CHECK_MODE=warn（仅告警）或 off（关闭检查）。",
        ]
    )
    return "\n".join(lines)


def run_schema_check(
    bind: Engine | None = None,
    metadata: MetaData | None = None,
    mode: str | None = None,
) -> list[DriftIssue]:
    """启动期入口：按 `SCHEMA_CHECK_MODE` 检查并决定是否中止启动。

    返回不一致项列表（空列表表示结构一致或检查被跳过），便于调用方/测试断言。
    """
    from ..config import settings

    resolved = (mode or getattr(settings, "schema_check_mode", MODE_STRICT) or MODE_STRICT).lower()
    if resolved not in _VALID_MODES:
        logger.warning("未知的 SCHEMA_CHECK_MODE=%s，按 strict 处理", resolved)
        resolved = MODE_STRICT

    if resolved == MODE_OFF:
        logger.info("Schema 自检已关闭（SCHEMA_CHECK_MODE=off）")
        return []

    try:
        issues = inspect_schema(bind, metadata)
    except SQLAlchemyError as exc:
        # 数据库不可用 ≠ 结构漂移：这里只提示，启动与否交给 /health 与后续请求暴露
        logger.warning("Schema 自检跳过：数据库不可用（%s）", exc)
        return []

    if not issues:
        logger.info("Schema 自检通过：ORM 模型与数据库表结构一致")
        return []

    report = format_drift_report(issues)
    if resolved == MODE_WARN:
        logger.error("%s\n（SCHEMA_CHECK_MODE=warn：仅告警，服务继续启动）", report)
        return issues

    logger.error("%s", report)
    raise SchemaDriftError(report)
