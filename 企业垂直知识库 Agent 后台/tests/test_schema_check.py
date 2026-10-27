"""启动期 Schema 自检（v1.7）回归测试。

覆盖四类关注点：
1. 检测准确：只缺列、缺表、全一致三种库状态下结论正确；
2. 报错可执行：报告里必须带能直接粘贴执行的 ALTER 语句；
3. 启动阻断：strict 下应用真正起不来（不是只打了行日志）；
4. 不误报：全新库上跑完整 ORM metadata 必须零告警，数据库连不上不算漂移。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.database import Base
from app.core.schema_check import (
    SchemaDriftError,
    format_drift_report,
    import_all_models,
    inspect_schema,
    run_schema_check,
)


@pytest.fixture()
def demo_metadata() -> MetaData:
    """一个独立的小 metadata，避免测试依赖真实业务模型的字段演化。"""
    md = MetaData()
    Table(
        "demo_items",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(32), nullable=False),
        Column("note", String(64), nullable=True),
        Column("seq", Integer, nullable=False),
    )
    return md


@pytest.fixture()
def sqlite_engine():
    engine = create_engine("sqlite://")
    yield engine
    engine.dispose()


def _create_legacy_table(engine, ddl: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(ddl))


# ---------------------------------------------------------------- 检测准确


def test_no_drift_on_fully_created_schema(sqlite_engine, demo_metadata):
    demo_metadata.create_all(sqlite_engine)
    assert inspect_schema(sqlite_engine, demo_metadata) == []


def test_detects_only_missing_columns(sqlite_engine, demo_metadata):
    # 模拟"存量老表"：只有 id / name 两列
    _create_legacy_table(
        sqlite_engine,
        "CREATE TABLE demo_items (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)",
    )

    issues = inspect_schema(sqlite_engine, demo_metadata)

    assert [i.target for i in issues] == ["demo_items.note", "demo_items.seq"]
    assert {i.kind for i in issues} == {"missing_column"}
    # 可空列：直接 ADD COLUMN ... NULL
    assert issues[0].ddl == "ALTER TABLE `demo_items` ADD COLUMN `note` VARCHAR(64) NULL;"
    assert issues[0].hint == ""


def test_not_null_without_default_gives_two_step_ddl(sqlite_engine, demo_metadata):
    """NOT NULL 且无默认值时，不能直接生成 ADD COLUMN ... NOT NULL（存量行会拒绝执行）。"""
    _create_legacy_table(
        sqlite_engine,
        "CREATE TABLE demo_items (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)",
    )

    seq = next(i for i in inspect_schema(sqlite_engine, demo_metadata) if i.column == "seq")

    assert seq.ddl == "ALTER TABLE `demo_items` ADD COLUMN `seq` INTEGER NULL;"
    assert "MODIFY COLUMN `seq` INTEGER NOT NULL" in seq.hint
    # 生成的 DDL 必须真的能在这张有数据的表上跑通
    with sqlite_engine.begin() as conn:
        conn.execute(text("INSERT INTO demo_items (id, name) VALUES (1, 'a')"))
        conn.execute(text(seq.ddl))


def test_missing_table_reported_with_bootstrap_hint(sqlite_engine, demo_metadata):
    issues = inspect_schema(sqlite_engine, demo_metadata)

    assert len(issues) == 1
    assert issues[0].kind == "missing_table"
    assert issues[0].table == "demo_items"
    assert "init_db.sql" in issues[0].hint


# ---------------------------------------------------------------- 报错可执行


def test_report_is_actionable(sqlite_engine, demo_metadata):
    _create_legacy_table(
        sqlite_engine,
        "CREATE TABLE demo_items (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)",
    )
    report = format_drift_report(inspect_schema(sqlite_engine, demo_metadata))

    assert "数据库结构落后于 ORM 模型" in report
    assert "2 个列缺失" in report
    assert "ALTER TABLE `demo_items` ADD COLUMN `note`" in report
    assert "[缺失列] demo_items.note" in report
    # 必须告诉排障的人怎么临时放行，否则只能去翻代码
    assert "SCHEMA_CHECK_MODE=warn" in report


# ---------------------------------------------------------------- 三种模式


def test_strict_mode_raises(sqlite_engine, demo_metadata):
    _create_legacy_table(
        sqlite_engine,
        "CREATE TABLE demo_items (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)",
    )

    with pytest.raises(SchemaDriftError) as exc:
        run_schema_check(sqlite_engine, demo_metadata, mode="strict")

    assert "missing" not in str(exc.value)  # 面向人的中文报告，不是英文堆栈
    assert "ALTER TABLE `demo_items`" in str(exc.value)


def test_warn_mode_logs_but_continues(sqlite_engine, demo_metadata, caplog):
    _create_legacy_table(
        sqlite_engine,
        "CREATE TABLE demo_items (id INTEGER PRIMARY KEY, name VARCHAR(32) NOT NULL)",
    )

    with caplog.at_level(logging.ERROR):
        issues = run_schema_check(sqlite_engine, demo_metadata, mode="warn")

    assert len(issues) == 2
    assert any("SCHEMA_CHECK_MODE=warn" in r.message for r in caplog.records)


def test_off_mode_skips_check(sqlite_engine, demo_metadata):
    assert run_schema_check(sqlite_engine, demo_metadata, mode="off") == []


def test_unreachable_database_is_not_drift(monkeypatch):
    """数据库连不上 ≠ 结构漂移：只警告并跳过，交给 /health 反映。"""

    def _boom(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("app.core.schema_check.inspect", _boom)

    assert run_schema_check(mode="strict") == []


# ---------------------------------------------------------------- 不误报 / 模型完整


def test_import_all_models_covers_eval_and_trace():
    """架构守卫：eval_* / trace_* 必须能被自检看到（它们不在 models/__init__ 里也能导入）。"""
    import_all_models()

    expected = {
        "users",
        "knowledge_bases",
        "documents",
        "document_chunks",
        "conversations",
        "messages",
        "message_feedbacks",
        "kb_permissions",
        "operation_logs",
        "eval_datasets",
        "eval_dataset_items",
        "eval_runs",
        "eval_run_scores",
        "trace_logs",
        "trace_tool_calls",
    }
    assert expected <= set(Base.metadata.tables)


def test_full_metadata_has_no_false_positive_on_fresh_db(sqlite_engine):
    """全新库建完表后必须零告警——否则每次启动都会误报，告警就失去意义了。"""
    import_all_models()
    Base.metadata.create_all(sqlite_engine)

    assert inspect_schema(sqlite_engine) == []


# ---------------------------------------------------------------- 启动流程接线


def test_startup_invokes_schema_check(monkeypatch):
    from app.main import app

    calls: list[object] = []
    monkeypatch.setattr(
        "app.main.run_schema_check", lambda *a, **k: calls.append(a[0] if a else None) or []
    )

    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200

    assert calls, "启动流程没有调用 Schema 自检"


def test_startup_aborts_on_drift(monkeypatch):
    """strict 模式下缺列必须让应用起不来，而不是只写一行日志。"""
    from app.main import app

    def _raise(*args, **kwargs):
        raise SchemaDriftError("数据库缺失列 demo.x，请执行 ALTER TABLE `demo` ADD COLUMN `x` INT NULL;")

    monkeypatch.setattr("app.main.run_schema_check", _raise)

    with pytest.raises(SchemaDriftError):
        with TestClient(app):
            pass
