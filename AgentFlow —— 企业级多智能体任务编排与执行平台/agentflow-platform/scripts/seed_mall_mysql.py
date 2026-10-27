"""在真实 MySQL 上建 mall 演示库（结构 + 确定性种子数据）。

用途：本地/联调想跑「真实 SQL」而非 `FakeSqlExecutor` 时，先把 mall 库准备好。

    python scripts/seed_mall_mysql.py --host 127.0.0.1 --port 3307 --user root --password ""
    # 只校验不写入
    python scripts/seed_mall_mysql.py --check --host 127.0.0.1 --port 3307 --user root --password ""

设计说明：
- 表/列名与 ``scripts/verify_seed.py`` 的 ``MALL_FIXTURE_DDL`` 对齐（orders/skus/ad_campaigns），
  另补 ``traffic_records`` 与 ``categories``（``SQL_ALLOWED_TABLES`` 里声明的五张表）；
- ``orders`` 额外提供 ``amount`` / ``order_date`` 两个 **生成列**（= gmv / dt）：
  真实 LLM 生成的 SQL 常用这两个列名，加别名列可避免因命名差异直接报错，
  同时不引入第二份数据（生成列由 MySQL 自身维护，不占业务口径）；
- 数据为确定性伪随机（固定 seed），且刻意保留 PRD 11.3 的埋因：
  「数码配件」2026-10-12~10-18 这一周 GMV 环比约 -18%；
- 幂等：重复执行会先 DROP 再建，不会叠加。
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

import pymysql

# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------
DDL = [
    "DROP TABLE IF EXISTS orders",
    "DROP TABLE IF EXISTS traffic_records",
    "DROP TABLE IF EXISTS ad_campaigns",
    "DROP TABLE IF EXISTS skus",
    "DROP TABLE IF EXISTS categories",
    """
    CREATE TABLE categories (
        category_id   INT PRIMARY KEY,
        category_name VARCHAR(64) NOT NULL,
        parent_id     INT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE skus (
        sku_id        INT PRIMARY KEY,
        sku_code      VARCHAR(32) NOT NULL UNIQUE,
        sku_name      VARCHAR(128) NOT NULL,
        category_id   INT,
        category_name VARCHAR(64),
        price         DECIMAL(12,2) DEFAULT 0,
        stock         INT DEFAULT 0,
        turnover_days INT DEFAULT 0,
        status        VARCHAR(16) DEFAULT '在售',
        KEY idx_sku_cat (category_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE orders (
        order_id   BIGINT PRIMARY KEY,
        sku_id     INT NOT NULL DEFAULT 0,
        category_id INT NOT NULL,
        channel    VARCHAR(16) NOT NULL DEFAULT '自营',
        pay_amount DECIMAL(14,2) NOT NULL DEFAULT 0,
        pay_status VARCHAR(16) NOT NULL DEFAULT 'paid',
        pay_time   DATETIME NOT NULL,
        order_cnt  INT NOT NULL DEFAULT 1,
        conv_rate  DECIMAL(6,3) NULL,
        dt         DATE NOT NULL,
        -- 生成列：真实模型对同一语义常有多种列名（gmv/amount/order_amount、
        -- dt/order_date、pay_time/create_time），这里全部映射到同源列，
        -- 避免「语义没错、列名不同」直接 SQL_SYNTAX。
        gmv          DECIMAL(14,2) GENERATED ALWAYS AS (pay_amount) STORED,
        amount       DECIMAL(14,2) GENERATED ALWAYS AS (pay_amount) STORED,
        order_amount DECIMAL(14,2) GENERATED ALWAYS AS (pay_amount) STORED,
        order_date   DATE GENERATED ALWAYS AS (dt) STORED,
        create_time  DATETIME GENERATED ALWAYS AS (pay_time) STORED,
        KEY idx_orders_dt (dt),
        KEY idx_orders_cat_dt (category_id, dt),
        KEY idx_orders_channel_dt (channel, dt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE traffic_records (
        id          BIGINT PRIMARY KEY AUTO_INCREMENT,
        category_id INT NOT NULL,
        dt          DATE NOT NULL,
        uv          INT NOT NULL DEFAULT 0,
        pv          INT NOT NULL DEFAULT 0,
        conv_rate   DECIMAL(6,3) NOT NULL DEFAULT 0,
        KEY idx_traffic_cat_dt (category_id, dt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE ad_campaigns (
        campaign_id   INT PRIMARY KEY,
        campaign_name VARCHAR(64) NOT NULL,
        category_id   INT NOT NULL,
        daily_budget  DECIMAL(12,2) NOT NULL DEFAULT 0,
        impressions   INT NOT NULL DEFAULT 0,
        clicks        INT NOT NULL DEFAULT 0,
        cost          DECIMAL(12,2) NOT NULL DEFAULT 0,
        dt            DATE NOT NULL,
        KEY idx_campaign_cat_dt (category_id, dt)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

CATEGORIES = [
    (1, "服饰鞋包", None),
    (2, "家居日用", None),
    (3, "数码配件", None),
    (4, "食品生鲜", None),
    (5, "美妆个护", None),
]

SKUS = [
    (10086, "SKU-10086", "数码配件爆款无线耳机", 3, "数码配件", 0, 21, "下架"),
    (10087, "SKU-10087", "数码配件快充头", 3, "数码配件", 120, 9, "在售"),
    (10088, "SKU-10088", "数码配件磁吸充电宝", 3, "数码配件", 860, 12, "在售"),
    (20001, "SKU-20001", "纯棉基础款T恤", 1, "服饰鞋包", 300, 12, "在售"),
    (20002, "SKU-20002", "轻薄羽绒马甲", 1, "服饰鞋包", 95, 18, "在售"),
    (30001, "SKU-30001", "原木收纳盒", 2, "家居日用", 88, 15, "在售"),
    (30002, "SKU-30002", "厨房硅胶铲套装", 2, "家居日用", 430, 10, "在售"),
    (40001, "SKU-40001", "冷萃咖啡液", 4, "食品生鲜", 1200, 6, "在售"),
    (40002, "SKU-40002", "云南高山蓝莓", 4, "食品生鲜", 260, 4, "在售"),
    (50001, "SKU-50001", "烟酰胺精华", 5, "美妆个护", 540, 11, "在售"),
    (50002, "SKU-50002", "氨基酸洁面乳", 5, "美妆个护", 780, 8, "在售"),
]

# 基线日 GMV（元）与订单量：品类量级差异贴近真实电商
BASE = {
    1: (9_200_000, 46_000, 3.4),
    2: (6_100_000, 50_800, 2.9),
    3: (10_400_000, 38_000, 1.7),
    4: (4_500_000, 64_200, 4.1),
    5: (3_200_000, 18_200, 2.2),
}

START = date(2026, 9, 1)
END = date(2026, 10, 18)
WEEK42 = (date(2026, 10, 12), date(2026, 10, 18))
DIP_CATEGORY = 3  # 数码配件：埋因 -18%
CHANNELS = ("天猫", "京东", "抖音", "小程序")
ORDERS_PER_CHANNEL = 3  # 每品类每天每渠道的订单数


def build_rows() -> tuple[list, list, list]:
    """生成订单级事实表（不是按天聚合），这样 COUNT(*) / AVG 类 SQL 才有意义。"""
    rng = random.Random(20261022)
    orders: list[tuple] = []
    traffic: list[tuple] = []
    campaigns: list[tuple] = []
    oid = 1
    cid = 1
    day = START
    while day <= END:
        for category_id, (base_gmv, base_cnt, base_conv) in BASE.items():
            factor = 1 + rng.uniform(-0.06, 0.06)
            if day.weekday() >= 5:  # 周末轻微放量
                factor *= 1.08
            if category_id == DIP_CATEGORY and WEEK42[0] <= day <= WEEK42[1]:
                factor *= 0.82  # PRD 11.3 埋因
            daily_gmv = base_gmv * factor
            daily_conv = round(base_conv * (0.94 + rng.uniform(0, 0.12)), 3)
            per_order = daily_gmv / (len(CHANNELS) * ORDERS_PER_CHANNEL)

            for channel in CHANNELS:
                # 渠道结构：天猫/京东为主，抖音增长中，小程序长尾
                weight = {"天猫": 1.25, "京东": 1.0, "抖音": 0.8, "小程序": 0.45}[channel]
                for _ in range(ORDERS_PER_CHANNEL):
                    amount = round(per_order * weight * rng.uniform(0.72, 1.3), 2)
                    hour = rng.randint(8, 23)
                    minute = rng.randint(0, 59)
                    pay_status = "paid" if rng.random() > 0.04 else rng.choice(("refunded", "unpaid"))
                    orders.append(
                        (
                            oid,
                            rng.choice([s[0] for s in SKUS if s[3] == category_id]),
                            category_id,
                            channel,
                            amount,
                            pay_status,
                            f"{day.isoformat()} {hour:02d}:{minute:02d}:00",
                            1,
                            daily_conv,
                            day.isoformat(),
                        )
                    )
                    oid += 1

            uv = int(base_cnt / (daily_conv / 100) * rng.uniform(0.85, 1.05))
            traffic.append((category_id, day.isoformat(), uv, uv * rng.randint(3, 6), daily_conv))

            budget = round(base_gmv / 1000 * rng.uniform(0.55, 0.85), 2)
            campaigns.append(
                (
                    cid,
                    f"{base_conv}-{category_id}-{day.strftime('%m%d')}",
                    category_id,
                    budget,
                    int(budget * rng.uniform(90, 140)),
                    int(budget * rng.uniform(1.2, 2.4)),
                    round(budget * rng.uniform(0.86, 1.0), 2),
                    day.isoformat(),
                )
            )
            cid += 1
        day += timedelta(days=1)

    # 第二遍：把「数码配件 W42 环比」精确校正到 -18%（PRD 11.3 埋因值），
    # 避免随机抖动导致评测口径对不上文档。
    w41 = sum(r[4] for r in orders if r[2] == DIP_CATEGORY and date.fromisoformat(r[9]) < WEEK42[0]
              and date.fromisoformat(r[9]) >= WEEK42[0] - timedelta(days=7))
    w42 = sum(r[4] for r in orders if r[2] == DIP_CATEGORY and WEEK42[0] <= date.fromisoformat(r[9]) <= WEEK42[1])
    if w41 and w42:
        scale = w41 * 0.82 / w42
        orders = [
            (r[0], r[1], r[2], r[3], round(r[4] * scale, 2), r[5], r[6], r[7], r[8], r[9])
            if (r[2] == DIP_CATEGORY and WEEK42[0] <= date.fromisoformat(r[9]) <= WEEK42[1])
            else r
            for r in orders
        ]
    return orders, traffic, campaigns


def seed(conn: pymysql.connections.Connection) -> dict[str, int]:
    orders, traffic, campaigns = build_rows()
    with conn.cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt)
        cur.executemany("INSERT INTO categories VALUES (%s,%s,%s)", CATEGORIES)
        cur.executemany(
            "INSERT INTO skus (sku_id, sku_code, sku_name, category_id, category_name, price, stock, turnover_days, status)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(s[0], s[1], s[2], s[3], s[4], 0, s[5], s[6], s[7]) for s in SKUS],
        )
        cur.executemany(
            "INSERT INTO orders (order_id, sku_id, category_id, channel, pay_amount, pay_status,"
            " pay_time, order_cnt, conv_rate, dt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            orders,
        )
        cur.executemany(
            "INSERT INTO traffic_records (category_id, dt, uv, pv, conv_rate) VALUES (%s,%s,%s,%s,%s)",
            traffic,
        )
        cur.executemany(
            "INSERT INTO ad_campaigns (campaign_id, campaign_name, category_id, daily_budget, impressions, clicks, cost, dt)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            campaigns,
        )
    conn.commit()
    return {"categories": len(CATEGORIES), "skus": len(SKUS), "orders": len(orders),
            "traffic_records": len(traffic), "ad_campaigns": len(campaigns)}


def check(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        for table in ("categories", "skus", "orders", "traffic_records", "ad_campaigns"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  {table:<16} {cur.fetchone()[0]:>6} 行")
        cur.execute(
            "SELECT c.category_name, ROUND(SUM(o.gmv)/10000, 1) AS gmv_wan "
            "FROM orders o JOIN categories c ON c.category_id=o.category_id "
            "WHERE o.dt BETWEEN '2026-10-12' AND '2026-10-18' GROUP BY c.category_name "
            "ORDER BY gmv_wan DESC"
        )
        print("  W42 各品类 GMV（万元）:")
        for name, gmv in cur.fetchall():
            print(f"    {name:<8} {gmv:>10}")
        cur.execute(
            "SELECT ROUND(SUM(CASE WHEN dt BETWEEN '2026-10-12' AND '2026-10-18' THEN gmv END),2),"
            "       ROUND(SUM(CASE WHEN dt BETWEEN '2026-10-05' AND '2026-10-11' THEN gmv END),2) "
            "FROM orders WHERE category_id=3"
        )
        w42, w41 = cur.fetchone()
        pct = (float(w42) / float(w41) - 1) * 100
        print(f"  数码配件 W42={w42} W41={w41} 环比 {pct:+.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="建 mall 演示库（真实 MySQL）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3307)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default="mall")
    parser.add_argument("--check", action="store_true", help="只校验现有数据，不重建")
    args = parser.parse_args()

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=args.password,
        database=args.database, charset="utf8mb4", autocommit=False,
    )
    try:
        if args.check:
            print(f"[check] {args.user}@{args.host}:{args.port}/{args.database}")
            check(conn)
            return 0
        counts = seed(conn)
        print("[seed] 已重建 " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        check(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
