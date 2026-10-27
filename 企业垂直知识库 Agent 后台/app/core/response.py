"""统一响应体工具。"""


def ok(data=None, message: str = "ok") -> dict:
    """构造成功响应：code=0。"""
    return {"code": 0, "message": message, "data": data}
