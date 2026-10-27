"""drop_all 安全守卫的回归测试（2026-10-27 真实库被清空事故后新增）。

事故复盘：`tests/` 里 6 个模块在 fixture 收尾执行 `Base.metadata.drop_all(bind=engine)`，
而 engine 指向真实 MySQL 的 kb_agent —— 一次全量 pytest 就把演示库 15 张表 DROP 重建，
用户/知识库/文档/切片全部清空，评测任务随之全线 401。

这两个用例只验证守卫的放行/拒绝规则，**不连数据库、不写任何数据**：
create_engine 是惰性的，守卫在真正取连接之前就抛错了。
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine


def test_guard_blocks_drop_on_real_database():
    """真实库（非 _test 结尾）必须拒绝 drop_all —— 且是抛错，不是静默跳过。"""
    engine = create_engine("mysql+pymysql://u:p@127.0.0.1:3306/kb_agent")  # 惰性，不建连接
    with pytest.raises(RuntimeError, match="已阻止"):
        MetaData().drop_all(bind=engine)


def test_guard_allows_test_suffix_database():
    """库名以 _test 结尾时放行。

    断言方式：被守卫拦下会抛 RuntimeError("已阻止")；放行后才会真的去连库，
    这里连的是不存在的 u:p@127.0.0.1，所以一定会拿到 OperationalError。
    只要不是"已阻止"，就说明守卫放行了 —— 全程不写任何数据。
    """
    engine = create_engine("mysql+pymysql://u:p@127.0.0.1:3306/kb_agent_test")
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - 放行后的连接错误即可
        MetaData().drop_all(bind=engine)
    assert "已阻止" not in str(excinfo.value)


def test_guard_allows_sqlite():
    """sqlite（单测默认环境）永远放行。"""
    engine = create_engine("sqlite:///:memory:")
    MetaData().drop_all(bind=engine)


def test_guard_escape_hatch(monkeypatch):
    """显式设置 KB_AGENT_ALLOW_DESTRUCTIVE_TESTS=1 时才允许对真实库 drop。"""
    monkeypatch.setenv("KB_AGENT_ALLOW_DESTRUCTIVE_TESTS", "1")
    engine = create_engine("mysql+pymysql://u:p@127.0.0.1:3306/kb_agent")
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        MetaData().drop_all(bind=engine)
    assert "已阻止" not in str(excinfo.value)
