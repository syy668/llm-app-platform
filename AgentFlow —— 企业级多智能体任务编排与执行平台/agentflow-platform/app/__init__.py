"""AgentFlow Platform — 企业级多智能体任务编排与执行平台。

``__version__`` 是**交付版本号的单一事实来源**：``/``、``/healthz``、
``/api/v1/health``、FastAPI 文档（/docs）与启动日志全部从这里读取。

缺陷 D-01 修复前这里是 W1 骨架遗留的 ``0.1.0-w1``，与 CHANGELOG 的当前版本脱节，
导致健康检查回显的版本号无法体现真实交付范围。现改为与 CHANGELOG「当前版本」
对齐（CHANGELOG 侧的 ``-wip`` 只是文档标记，比较时忽略），
并由 ``tests/test_version_alignment.py`` 做回归校验，防止再次落后。
"""

__version__ = "1.3.0"
