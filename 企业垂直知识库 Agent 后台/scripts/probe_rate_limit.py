"""真实 Redis 下的 AI 限流在线验证脚本（批次3 交付物）。

作用：对着**正在运行的后端**高频请求 `/kbs/{id}/query`，直到出现 429，
证明「真实 Redis + 令牌桶」这条路真的通了（而不仅仅是单元测试里通）。

典型用法（后端已在 8000 运行、.env 里 FAKE_REDIS=false）：

    # 1) 用管理员账号登录后压测（推荐，最贴近真实调用）
    ./.venv/Scripts/python.exe scripts/probe_rate_limit.py --username admin --password 你的密码

    # 2) 没有密码时，脚本会直接用 DB 里的用户铸造一个 session（仅限本机排查）
    ./.venv/Scripts/python.exe scripts/probe_rate_limit.py

    # 3) 想指定知识库 / 阈值 / 次数
    ./.venv/Scripts/python.exe scripts/probe_rate_limit.py --kb-id 6 --limit 3 --count 6

行为说明：
- 先读 `/health`，若 `redis_backend=fake` 或 `rate_limit!=enabled` 立即报错退出（提示改 .env）；
- 通过 `PUT /system/config` 临时把 `ai_rate_limit_per_minute` 调到 `--limit`，
  结束时**必定还原**原值（finally 中恢复，异常/中断也会执行）；
- 每次请求都会真实走 RAG + LLM（mock 模式下是本地规则回答），所以 `--count` 不要设太大；
- 退出码：0=如期出现 429；2=环境不满足（fake redis / 无可用知识库）；3=未触发 429（防护未生效）。
"""
import argparse
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _config_file() -> Path:
    """定位系统配置文件（与 app.core.runtime_config 同一口径，仅用于提示/判断）。"""
    custom = os.getenv("SYSTEM_CONFIG_FILE", "").strip()
    return Path(custom) if custom else ROOT / "data" / "system_config.json"


def _override_keys() -> set[str]:
    """读取配置文件里已显式保存的键，用于判断"原本是否存在阈值覆盖值"。"""
    import json

    try:
        raw = json.loads(_config_file().read_text(encoding="utf-8"))
        return set(raw.keys()) if isinstance(raw, dict) else set()
    except Exception:  # noqa: BLE001 - 文件缺失/损坏都视为无覆盖值
        return set()


def _drop_override_key(key: str) -> bool:
    """从配置文件里删除某个覆盖键（原本就没有该键时，验证结束用它恢复原貌）。

    说明：删除文件键不会改变"当前进程内"的生效值（已是原默认值），
    只影响下次启动的取值来源，回到「跟随 .env」的口径。
    """
    import json

    path = _config_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or key not in raw:
            return False
        raw.pop(key)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def _mint_session(username: str) -> tuple[str, int, str]:
    """本机排查用：不校验密码，直接用 DB 用户铸造 session。

    仅在「拿不到密码」时使用。会加载 app 配置（因此需在项目根目录、且 .env 可读）。
    """
    from app.config import settings  # noqa: F401 - 触发 .env 加载
    from app.core import security
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise SystemExit(f"[!] 用户 {username} 不存在")
        token = security.create_session({
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "status": user.status,
        })
        return token, user.id, user.role
    finally:
        db.close()


def _login(base: str, username: str, password: str) -> tuple[str, int, str]:
    resp = httpx.post(
        f"{base}/auth/login",
        json={"username": username, "password": password},
        timeout=15,
        trust_env=False,
    )
    body = resp.json()
    if resp.status_code != 200 or body.get("code") != 0:
        raise SystemExit(f"[!] 登录失败: HTTP {resp.status_code} {body.get('message')}")
    data = body["data"]
    role = data.get("user", {}).get("role", "")
    uid = data.get("user", {}).get("id", 0)
    return data["access_token"], uid, role


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 Redis 限流在线验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.getenv("ADMIN_INIT_PASSWORD", ""))
    parser.add_argument("--kb-id", type=int, default=0, help="缺省自动取第一个可访问知识库")
    parser.add_argument("--limit", type=int, default=3, help="临时调低的每分钟上限")
    parser.add_argument("--count", type=int, default=0, help="请求次数，缺省 = limit + 2")
    parser.add_argument("--question", default="打印机卡纸怎么办")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    count = args.count or args.limit + 2
    client = httpx.Client(base_url=base, timeout=60, trust_env=False)  # 绕过系统代理

    # 1) 环境自检
    health = client.get("/health").json()
    data = health.get("data", {})
    print(f"[health] status={data.get('status')} db={data.get('db')} redis={data.get('redis')} "
          f"backend={data.get('redis_backend')} rate_limit={data.get('rate_limit')}")
    if data.get("redis_backend") != "real":
        print("[!] 当前是 fakeredis 内存后端，限流被跳过。请把 .env 的 FAKE_REDIS 改为 false 后重启后端。")
        return 2
    if data.get("rate_limit") != "enabled":
        print(f"[!] 限流状态为 {data.get('rate_limit')}（Redis 不可用），请检查 Redis 服务。")
        return 2

    # 2) 取 token
    if args.password:
        token, uid, role = _login(base, args.username, args.password)
        print(f"[auth] 登录成功 user={args.username} id={uid} role={role}")
    else:
        token, uid, role = _mint_session(args.username)
        print(f"[auth] 已用 DB 用户 {args.username} 铸造 session（id={uid} role={role}，仅本机排查用）")
    headers = {"Authorization": f"Bearer {token}"}

    # 3) 选知识库
    kb_id = args.kb_id
    if not kb_id:
        kbs = client.get("/kbs", headers=headers).json().get("data", {})
        items = kbs.get("items") or kbs.get("list") or []
        if not items:
            print("[!] 当前账号下没有可访问的知识库，请用 --kb-id 指定或先创建知识库。")
            return 2
        kb_id = items[0]["id"]

    # 4) 临时调低阈值（结束时还原）
    original = client.get("/system/config", headers=headers).json().get("data", {})
    prev_limit = original.get("ai_rate_limit_per_minute")
    had_override = "ai_rate_limit_per_minute" in _override_keys()
    changed = False
    if role == "admin" and prev_limit == args.limit:
        print(f"[config] 阈值已是 {args.limit}/分钟，无需修改")
    elif role == "admin":
        put = client.put("/system/config", headers=headers, json={"ai_rate_limit_per_minute": args.limit})
        if put.status_code == 200 and put.json().get("code") == 0:
            changed = True
            print(f"[config] ai_rate_limit_per_minute: {prev_limit} -> {args.limit}（结束自动还原）")
        else:
            print(f"[config] 修改阈值失败（HTTP {put.status_code}），沿用当前值继续压测")
    else:
        print("[config] 非管理员，跳过阈值修改（沿用当前值）")

    hit_429 = False
    try:
        print(f"\n[stress] 对 /kbs/{kb_id}/query 连续请求 {count} 次（上限 {args.limit}/分钟）")
        for i in range(1, count + 1):
            resp = client.post(f"/kbs/{kb_id}/query", headers=headers, json={"question": args.question})
            body = {}
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                pass
            mark = "429 限流命中" if resp.status_code == 429 else ""
            print(f"  #{i:<3} HTTP {resp.status_code}  code={body.get('code')} "
                  f"msg={str(body.get('message'))[:24]} {mark}")
            if resp.status_code == 429:
                hit_429 = True
                assert body.get("code") == 429, "429 响应的业务码也应为 429"
                break
    finally:
        if changed:
            client.put("/system/config", headers=headers, json={"ai_rate_limit_per_minute": prev_limit})
            print(f"[config] 已还原 ai_rate_limit_per_minute={prev_limit}")
            if not had_override and _drop_override_key("ai_rate_limit_per_minute"):
                print(
                    f"[config] 已从 {_config_file().name} 移除临时写入的覆盖键，"
                    "恢复「跟随 .env 的 AI_RATE_LIMIT_PER_MINUTE」"
                )
        client.close()

    if hit_429:
        print("\n[OK] 真实 Redis 令牌桶限流已生效：高频请求被拦为 429。")
        return 0
    print("\n[FAIL] 达到预期次数仍未触发 429，限流可能未生效，请检查 rate_limit_ai 是否挂载。")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
