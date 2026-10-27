"""工具调用重试装饰器单元测试。"""
import pytest

from app.agent.tools.retry import with_retry


def test_retry_then_success(monkeypatch):
    monkeypatch.setenv("TOOL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("TOOL_RETRY_DELAY_SECONDS", "0")
    calls = {"n": 0}

    @with_retry
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_exhausted(monkeypatch):
    monkeypatch.setenv("TOOL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TOOL_RETRY_DELAY_SECONDS", "0")
    calls = {"n": 0}

    @with_retry
    def always_fail():
        calls["n"] += 1
        raise RuntimeError("always")

    with pytest.raises(RuntimeError):
        always_fail()
    assert calls["n"] == 2


def test_retry_success_first_try(monkeypatch):
    monkeypatch.setenv("TOOL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("TOOL_RETRY_DELAY_SECONDS", "0")
    calls = {"n": 0}

    @with_retry
    def ok_fn():
        calls["n"] += 1
        return "done"

    assert ok_fn() == "done"
    assert calls["n"] == 1
