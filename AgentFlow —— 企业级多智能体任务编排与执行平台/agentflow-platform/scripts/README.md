# scripts/ — 数据脚本目录（W1 仅占位）

| 文件 | 状态 | 说明 | 交付周 |
|---|---|---|---|
| `verify_seed.py` | 占位（仅 CLI + 断言清单） | 校验 mall 模拟数据 3 条埋因断言 + 20 个抽样断点，通过输出 `SEED VERIFIED` | W2 |
| `seed_mall_data.py`（待建） | 未创建 | 生成 orders / traffic_records / ad_campaigns / skus / categories，seed=42 | W2 |
| `seed_users.py`（待建） | 未创建 | 初始化 admin / approver / viewer 演示账号 | W2 |
| `ingest_weekly_reports.py`（待建） | 未创建 | 20 份历史周报经项目一上传接口灌入 tenant=agentflow | W2 |

W1 只交付 `verify_seed.py` 的骨架：CLI 参数、断言清单、退出码约定；
不实现任何 SQL 校验与数据生成逻辑。

```bash
# 查看断言清单（不需要数据库）
python scripts/verify_seed.py --dry-run

# W2 之后的真实校验
python scripts/verify_seed.py --dsn "$MYSQL_DSN" --seed 42
```
