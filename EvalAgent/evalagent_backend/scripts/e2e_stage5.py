"""第五步端到端：裁判配置管理（真实调用，不打桩）。

验证链条（顺序即防呆顺序）：
  C1 无 token 写 → 401
  C2 管理员读 → 200 且密钥为掩码（响应体里不许出现明文）
  C3 温度只读锁 0（响应 temperature=0.0 / temperature_locked=true）
  C4 非法模型名 → 422 且**不落库**（最关键的防呆）
  C5 非法 api_base → 422 且不落库（连探活都不该走）
  C6 合法模型 qwen-plus → 200，落库 + 裁判客户端按新签名重建（日志可证）
  C7 探活接口用新配置通过（model 回显 qwen-plus）
  C8 掩码提交 = 不修改密钥（密钥行不被改写）
  C9 回滚到原模型 deepseek-v4.1-flash-1017 → 200，日志再次重建
  C10 清理 runtime_configs，恢复「沿用 .env」基线

用法：.venv/Scripts/python.exe scripts/e2e_stage5.py [--keep]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("EVAL_BASE", "http://127.0.0.1:8200/api/v1")
UPSTREAM = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:8010/api/v1")
USERNAME = os.getenv("ADMIN_USER", "agentflow_svc")
PASSWORD = os.getenv("ADMIN_PASS", "Af-svc-2026!kb")
ALT_MODEL = "qwen-plus"          # 探活可用的备用模型（用于验证真实写入）
BACKUP_MODEL = "deepseek-v4.1-flash-1017"

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "e2e_stage5.log"

PASS, FAIL = 0, 0
lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    lines.append(text)


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        log(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        log(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))
    return ok


def api(method: str, path: str, token: str | None = None, base: str = BASE, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{base}{path}"
    with httpx.Client(timeout=120.0) as c:
        r = c.request(method, url, headers=headers, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:200]}
    return r.status_code, body


def db_rows(model: str) -> dict:
    """直连库看落库实况（绕过服务端缓存，避免「接口说成功但库没写」）。"""
    sys.path.insert(0, str(ROOT))
    from app.core.database import SessionLocal
    from app.models.runtime_config import RuntimeConfig

    db = SessionLocal()
    try:
        rows = db.query(RuntimeConfig).all()
        return {r.config_key: {"value": r.config_value, "by": r.updated_by} for r in rows}
    finally:
        db.close()


def main() -> int:
    log("=" * 78)
    log("第五步端到端：裁判配置管理（runtime_configs + 热更新 + 保存即探活）")
    log(f"评测后端 {BASE} / 身份上游 {UPSTREAM}")
    log("=" * 78)

    # 拿管理员 token（身份归项目一）
    status, body = api("POST", "/auth/login", base=UPSTREAM, json={"username": USERNAME, "password": PASSWORD})
    token = (body.get("data") or {}).get("access_token") if isinstance(body, dict) else None
    if not token:
        log(f"登录失败，无法继续：HTTP {status} {str(body)[:200]}")
        return 1
    # 顺带确认该账号确实是 admin（否则下面 403 是预期行为，不是 bug）
    _, me = api("GET", "/auth/me", token=token, base=UPSTREAM)
    role = ((me.get("data") or {}) if isinstance(me, dict) else {}).get("role")
    log(f"[0] 身份：{USERNAME} role={role} token={len(token)} 字符")

    before = db_rows("")
    log(f"    起始库内配置行：{sorted(before) or '（空 = 全部沿用 .env）'}")

    # ---- C1 无 token 写 ----
    status, body = api("PUT", "/system/judge-config", json={"model": "x"})
    check("C1 无 token 写 → 401", status == 401, f"HTTP {status} code={body.get('code')}")

    # ---- C2 读 + 掩码 ----
    status, body = api("GET", "/system/judge-config", token=token)
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    raw_text = json.dumps(body, ensure_ascii=False)
    plain_key = os.getenv("JUDGE_KEY", "")
    leaked = bool(plain_key and plain_key in raw_text)
    check(
        "C2 管理员读 200 且密钥掩码",
        status == 200 and data.get("api_key") in ("", "******") and not leaked,
        f"HTTP {status} key={data.get('api_key')} 明文泄漏={leaked}",
    )

    # ---- C3 温度锁 ----
    check(
        "C3 温度锁死 0（只读）",
        data.get("temperature") == 0.0 and data.get("temperature_locked") is True,
        f"temperature={data.get('temperature')} locked={data.get('temperature_locked')}",
    )
    log(f"    只读项：{json.dumps(data.get('readonly'), ensure_ascii=False)}")

    # ---- C4 非法模型名 ----
    status, body = api("PUT", "/system/judge-config", token=token, json={"model": "no-such-model-xyz"})
    after_bad = db_rows("")
    check(
        "C4 非法模型名 → 422 且不落库",
        status == 422 and after_bad == before,
        f"HTTP {status} 库内行变化={sorted(set(after_bad) - set(before)) or '无'}",
    )
    log(f"    后端原因：{str(body.get('message'))[:120]}")

    # ---- C5 非法 api_base（不该走到探活）----
    status, body = api("PUT", "/system/judge-config", token=token, json={"api_base": "dashscope.aliyuncs.com"})
    after_bad2 = db_rows("")
    check(
        "C5 非法接口地址 → 422 且不落库",
        status == 422 and after_bad2 == before,
        f"HTTP {status} 库内行变化={sorted(set(after_bad2) - set(before)) or '无'}",
    )

    # ---- C6 合法模型：真实写入 ----
    t0 = time.time()
    status, body = api("PUT", "/system/judge-config", token=token, json={"model": ALT_MODEL})
    cost = time.time() - t0
    payload = (body.get("data") or {}) if isinstance(body, dict) else {}
    after_ok = db_rows("")
    check(
        "C6 合法模型 → 200，落库并热生效",
        status == 200 and after_ok.get("JUDGE_MODEL", {}).get("value") == ALT_MODEL,
        f"HTTP {status} 探活 {payload.get('preflight', {}).get('latency_ms')}ms 总耗时 {cost:.1f}s",
    )
    check(
        "C6b 落库记录带修改人（可审计）",
        after_ok.get("JUDGE_MODEL", {}).get("by") == USERNAME,
        f"updated_by={after_ok.get('JUDGE_MODEL', {}).get('by')}",
    )

    # ---- C7 探活接口读新配置 ----
    status, body = api("POST", "/system/judge-config/preflight", token=token)
    pf = (body.get("data") or {}) if isinstance(body, dict) else {}
    check(
        "C7 探活新配置通过且回显新模型",
        status == 200 and pf.get("ok") is True and pf.get("model") == ALT_MODEL,
        f"ok={pf.get('ok')} model={pf.get('model')} {pf.get('latency_ms')}ms",
    )

    # ---- C8 掩码提交 = 不改密钥 ----
    key_before = after_ok.get("JUDGE_API_KEY", {}).get("value")
    status, _ = api("PUT", "/system/judge-config", token=token, json={"api_key": "******"})
    key_after = db_rows("").get("JUDGE_API_KEY", {}).get("value")
    check(
        "C8 提交掩码 = 不修改密钥",
        key_after == key_before,
        f"密钥行{'未变' if key_after == key_before else '被改写'}（HTTP {status}）",
    )

    # ---- C9 回滚 ----
    status, body = api("PUT", "/system/judge-config", token=token, json={"model": BACKUP_MODEL})
    rollback = db_rows("")
    check(
        "C9 回滚到原模型成功",
        status == 200 and rollback.get("JUDGE_MODEL", {}).get("value") == BACKUP_MODEL,
        f"当前 model={rollback.get('JUDGE_MODEL', {}).get('value')}",
    )
    # 客户端是懒重建（下次打分/探活才建）：这里主动探活一次，让回滚也落到日志上
    api("POST", "/system/judge-config/preflight", token=token)

    log("")
    log("-" * 78)
    log("热更新证据：服务日志里应有按新签名重建客户端的记录")
    uvicorn_log = ROOT / "logs" / "uvicorn.log"
    if uvicorn_log.exists():
        text = uvicorn_log.read_text(encoding="utf-8", errors="ignore")
        rebuilt_alt = f"裁判就绪：model={ALT_MODEL}" in text
        rebuilt_back = f"裁判就绪：model={BACKUP_MODEL}" in text
        check("C10 日志证实裁判客户端已重建（qwen-plus）", rebuilt_alt, "grep 裁判就绪：model=qwen-plus")
        check("C10b 日志证实已回滚重建（deepseek）", rebuilt_back, "grep 裁判就绪：model=deepseek")
    log("-" * 78)

    log("")
    log(f"结果：{PASS}/{PASS + FAIL} 通过" + ("（全部通过）" if FAIL == 0 else "（存在失败项）"))

    if "--keep" not in sys.argv:
        # 恢复基线：删掉运行时行，回到「沿用 .env」；并重启由调用方负责（缓存 TTL 30s 内仍生效）
        sys.path.insert(0, str(ROOT))
        from app.core.database import SessionLocal
        from app.models.runtime_config import RuntimeConfig
        from app.services import runtime_config as rc

        db = SessionLocal()
        try:
            deleted = db.query(RuntimeConfig).delete()
            db.commit()
        finally:
            db.close()
        rc.invalidate()
        log(f"[清理] 已删除 {deleted} 行 runtime_configs，回到「沿用 .env」基线")
    LOG.write_text("\n".join(lines), encoding="utf-8")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
