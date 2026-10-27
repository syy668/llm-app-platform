"""业务数据库查询工具：查询模拟业务库（设备 / 工单 / 告警）。

说明：
- 首次调用会自动建表并写入少量示例数据（模拟业务数据）。
- 对用户问题做关键词意图识别，选择对应表返回结构化结果文本。
- 仅执行只读 SELECT，不接受用户输入的任意 SQL，避免注入风险。
"""
import logging

from sqlalchemy import text

from ...core.database import SessionLocal
from .retry import with_retry

logger = logging.getLogger(__name__)

# 【v1.9 修补】业务库只有「设备 / 工单 / 告警」三张表。问题里没有任何一张表的关键词时，
# 这里返回的是「未找到」兜底文案。调用方（report_node 等）必须据此判定"没有这份数据"，
# 绝不能把它包装成报表或当成结论输出——伪报表比直接说"没找到"危害更大。
NO_BUSINESS_DATA_MARK = "业务库未找到与问题匹配的数据表"


def no_business_data_text(dev: int, tic: int, alt: int) -> str:
    """业务库无匹配数据时的统一说明文案（纯函数，便于单测）。

    概览数字只用于提示"可选范围"，不带任何"这就是答案"的暗示。
    """
    return (
        f"{NO_BUSINESS_DATA_MARK}：当前仅提供「设备 / 工单 / 告警」三类数据，"
        f"无法回答该问题。（现有数据概览：设备 {dev} 台，工单 {tic} 条，告警 {alt} 条）"
        "请在问题中明确“设备/工单/告警”以便精确查询。"
    )

# 建表 DDL（模拟业务库，列结构对齐设计文档，ENUM 统一用 VARCHAR 简化）
_BIZ_DDL = [
    """
    CREATE TABLE IF NOT EXISTS biz_devices (
      id            BIGINT NOT NULL AUTO_INCREMENT,
      device_name   VARCHAR(128) NOT NULL,
      device_type   VARCHAR(64),
      ip            VARCHAR(64),
      location      VARCHAR(128),
      vendor        VARCHAR(64),
      status        VARCHAR(16) NOT NULL DEFAULT 'online',
      cpu_usage     FLOAT,
      memory_usage  FLOAT,
      owner_dept    VARCHAR(64),
      updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      KEY idx_dev_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS biz_tickets (
      id          BIGINT NOT NULL AUTO_INCREMENT,
      ticket_no   VARCHAR(64) NOT NULL,
      title       VARCHAR(255) NOT NULL,
      priority    VARCHAR(16) NOT NULL DEFAULT 'P3',
      status      VARCHAR(16) NOT NULL DEFAULT 'open',
      assignee    VARCHAR(64),
      device_id   BIGINT,
      description TEXT,
      created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      resolved_at DATETIME,
      PRIMARY KEY (id),
      KEY idx_ticket_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS biz_alerts (
      id          BIGINT NOT NULL AUTO_INCREMENT,
      alert_name  VARCHAR(255) NOT NULL,
      severity    VARCHAR(16) NOT NULL DEFAULT 'warning',
      source      VARCHAR(128),
      device_id   BIGINT,
      content     TEXT,
      status      VARCHAR(16) NOT NULL DEFAULT 'firing',
      created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (id),
      KEY idx_alert_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

# 示例数据（仅当表为空时写入一次）
_DEVICE_SEED = [
    ("核心交换机-01", "交换机", "10.0.0.1", "机房A", "华为", "online", 35.0, 42.0, "网络部"),
    ("数据库服务器-01", "服务器", "10.0.0.10", "机房B", "浪潮", "online", 92.0, 78.0, "运维部"),
    ("出口防火墙-01", "防火墙", "10.0.0.254", "机房A", "深信服", "fault", 0.0, 0.0, "安全部"),
]
_TICKET_SEED = [
    ("TK2026001", "数据库服务器CPU持续告警", "P1", "processing", "张三", 2, "CPU 使用率超过 90% 持续 5 分钟"),
    ("TK2026002", "交换机端口DOWN处理", "P2", "open", "李四", 1, "核心交换机 GE0/0/1 端口 DOWN"),
    ("TK2026003", "磁盘空间清理", "P3", "resolved", "王五", 2, "磁盘低于 10% 需清理归档日志"),
]
_ALERT_SEED = [
    ("CPU使用率过高", "critical", "zabbix", 2, "数据库服务器 CPU 使用率 92%", "firing"),
    ("交换机端口DOWN", "major", "zabbix", 1, "核心交换机 GE0/0/1 端口 DOWN", "firing"),
    ("磁盘空间不足", "minor", "zabbix", 2, "磁盘剩余空间低于 10%", "recovered"),
]


def _ensure_tables(db) -> None:
    """建表 + 首次写入示例数据（幂等）。"""
    for ddl in _BIZ_DDL:
        db.execute(text(ddl))
    db.commit()

    if db.execute(text("SELECT COUNT(*) FROM biz_devices")).scalar() == 0:
        for row in _DEVICE_SEED:
            db.execute(
                text(
                    "INSERT INTO biz_devices (device_name, device_type, ip, location, vendor, status, cpu_usage, memory_usage, owner_dept) "
                    "VALUES (:n, :t, :ip, :loc, :v, :st, :cpu, :mem, :dept)"
                ),
                {"n": row[0], "t": row[1], "ip": row[2], "loc": row[3], "v": row[4], "st": row[5], "cpu": row[6], "mem": row[7], "dept": row[8]},
            )
    if db.execute(text("SELECT COUNT(*) FROM biz_tickets")).scalar() == 0:
        for row in _TICKET_SEED:
            db.execute(
                text(
                    "INSERT INTO biz_tickets (ticket_no, title, priority, status, assignee, device_id, description) "
                    "VALUES (:no, :ti, :pr, :st, :asg, :did, :desc)"
                ),
                {"no": row[0], "ti": row[1], "pr": row[2], "st": row[3], "asg": row[4], "did": row[5], "desc": row[6]},
            )
    if db.execute(text("SELECT COUNT(*) FROM biz_alerts")).scalar() == 0:
        for row in _ALERT_SEED:
            db.execute(
                text(
                    "INSERT INTO biz_alerts (alert_name, severity, source, device_id, content, status) "
                    "VALUES (:n, :sv, :src, :did, :ct, :st)"
                ),
                {"n": row[0], "sv": row[1], "src": row[2], "did": row[3], "ct": row[4], "st": row[5]},
            )
    db.commit()


def _rows_to_text(columns: list[str], rows) -> str:
    """把查询结果格式化为文本。"""
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(str(v) if v is not None else "-" for v in row))
    return "\n".join(lines)


@with_retry
def query_business_db(question: str) -> str:
    """根据问题关键词查询对应业务表，返回结构化文本结果。"""
    db = SessionLocal()
    try:
        _ensure_tables(db)
        q = question or ""

        # 关键词意图识别，选择对应表
        if any(k in q for k in ("设备", "服务器", "交换机", "防火墙", "device")):
            columns = ["id", "device_name", "device_type", "ip", "status", "cpu_usage", "memory_usage"]
            rows = db.execute(
                text("SELECT id, device_name, device_type, ip, status, cpu_usage, memory_usage FROM biz_devices ORDER BY id LIMIT 20")
            ).fetchall()
            return "业务库【设备表】查询结果：\n" + _rows_to_text(columns, rows)

        if any(k in q for k in ("工单", "ticket")):
            columns = ["id", "ticket_no", "title", "priority", "status", "assignee", "created_at"]
            rows = db.execute(
                text("SELECT id, ticket_no, title, priority, status, assignee, created_at FROM biz_tickets ORDER BY id LIMIT 50")
            ).fetchall()
            return "业务库【工单表】查询结果：\n" + _rows_to_text(columns, rows)

        if any(k in q for k in ("告警", "alert")):
            columns = ["id", "alert_name", "severity", "source", "status", "content"]
            rows = db.execute(
                text("SELECT id, alert_name, severity, source, status, content FROM biz_alerts ORDER BY id LIMIT 20")
            ).fetchall()
            return "业务库【告警表】查询结果：\n" + _rows_to_text(columns, rows)

        # 无法识别时返回「未找到」标记 + 现有数据概览（概览仅用于提示可选范围，
        # 不带任何"这就是答案"的暗示；调用方见 NO_BUSINESS_DATA_MARK 即知无匹配）
        dev = db.execute(text("SELECT COUNT(*) FROM biz_devices")).scalar()
        tic = db.execute(text("SELECT COUNT(*) FROM biz_tickets")).scalar()
        alt = db.execute(text("SELECT COUNT(*) FROM biz_alerts")).scalar()
        return no_business_data_text(dev, tic, alt)
    except Exception as exc:
        logger.exception("业务库查询失败")
        return f"业务库查询失败：{exc}"
    finally:
        db.close()
