"""导出 OpenAPI（Swagger）JSON，供接口文档与 Apifox/Postman 导入使用。

用法（离线，无需 MySQL/Redis/LLM）：
    python scripts/export_openapi.py
输出：docs/openapi.json
"""
import json
import os
import sys
from pathlib import Path

# 离线环境变量（与 tests/conftest.py 同策略，必须在 import app 之前注入）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "export-openapi-secret")
os.environ.setdefault("FAKE_REDIS", "true")
os.environ.setdefault("LLM_PROVIDER", "mock")

from app.main import app  # noqa: E402

out_dir = ROOT / "docs"
out_dir.mkdir(exist_ok=True)
out_file = out_dir / "openapi.json"
spec = app.openapi()
out_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

# 简要统计
paths = spec.get("paths", {})
count = sum(len([m for m in ops if m in {"get", "post", "put", "delete", "patch"}]) for ops in paths.values())
print(f"已导出 {out_file}")
print(f"路径数: {len(paths)}，接口数: {count}")
