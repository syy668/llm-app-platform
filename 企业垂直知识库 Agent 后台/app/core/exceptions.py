"""业务异常类定义。

业务代码抛出 BizError 及其子类；全局异常处理器
（app/core/exception_handler.py）将其统一转换为
{code, message, data} 结构返回，HTTP 状态码与错误码对齐：
  - 401 未登录 / token 无效
  - 403 已登录但越权
  - 404 资源不存在
  - 409 资源冲突（如用户名重复）

注：处理器注册逻辑已迁移至 app/core/exception_handler.py，
main.py 从该模块导入 register_exception_handlers。
"""


class BizError(Exception):
    """业务异常基类。"""

    def __init__(self, code: int, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class UnauthorizedError(BizError):
    """未登录 / token 无效，HTTP 401。"""

    def __init__(self, message: str = "未登录或登录已过期"):
        super().__init__(401, message, 401)


class ForbiddenError(BizError):
    """已登录但无权限（越权），HTTP 403。"""

    def __init__(self, message: str = "无权限访问该资源"):
        super().__init__(403, message, 403)


class NotFoundError(BizError):
    """资源不存在，HTTP 404。"""

    def __init__(self, message: str = "资源不存在"):
        super().__init__(404, message, 404)


class ConflictError(BizError):
    """资源冲突（如用户名重复），HTTP 409。"""

    def __init__(self, message: str = "资源冲突"):
        super().__init__(409, message, 409)
