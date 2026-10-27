"""遍历 Vite dev server 的模块图，找出「200 但返回 0 字节」或非 200 的模块。

用法： python scripts/check_vite_modules.py [base_url]
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5173").rstrip("/")
ENTRY = "/src/main.ts"
MAX_MODULES = 600

# 静态 import / re-export / 动态 import / 副作用 import
PATTERNS = [
    re.compile(r"""\bfrom\s*["']([^"']+)["']"""),
    re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""\bimport\s*["']([^"']+)["']"""),
]

NON_CODE_SUFFIX = (".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2")


def fetch(path: str) -> tuple[int, bytes]:
    url = BASE + path
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except Exception as exc:  # noqa: BLE001 - 探活脚本需容错
        return -1, str(exc).encode()


def resolve(spec: str, from_path: str) -> str | None:
    """把 import 说明符解析成 dev server 上的绝对路径。"""
    if spec.startswith(("http://", "https://", "//", "data:", "virtual:")):
        return None
    if spec.startswith("/"):
        return spec
    if spec.startswith("./") or spec.startswith("../"):
        base = from_path.split("?")[0]
        joined = urllib.parse.urljoin(base, spec)
        return joined
    # 裸模块名（理论上 Vite 已重写成 /node_modules/.vite/... 或 /@id/）
    return None


def main() -> int:
    seen: set[str] = set()
    queue: deque[str] = deque([ENTRY])
    problems: list[tuple[str, str]] = []
    empties: list[str] = []

    while queue and len(seen) < MAX_MODULES:
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)

        status, body = fetch(path)
        if status != 200:
            problems.append((path, f"HTTP {status}"))
            continue
        if not body:
            empties.append(path)
            continue
        if path.split("?")[0].endswith(NON_CODE_SUFFIX):
            continue

        text = body.decode("utf-8", "replace")
        for pat in PATTERNS:
            for spec in pat.findall(text):
                target = resolve(spec, path)
                if target and target not in seen:
                    queue.append(target)

    print(f"入口        : {ENTRY}")
    print(f"已扫描模块  : {len(seen)}")
    print(f"非 200      : {len(problems)}")
    for path, why in problems[:20]:
        print(f"  [FAIL] {path} -> {why}")
    print(f"0 字节模块  : {len(empties)}")
    for path in empties[:40]:
        print(f"  [EMPTY] {path}")

    ok = not problems
    print("\n结论:", "全部模块可加载" if ok else "存在加载失败模块")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
