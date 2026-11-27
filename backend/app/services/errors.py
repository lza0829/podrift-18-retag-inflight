from __future__ import annotations


class ApiError(Exception):
    """业务错误：穿透 db_write 上下文以触发整笔回滚，再由异常处理器转成 JSON。"""

    def __init__(self, message: str, conflict: bool = False):
        super().__init__(message)
        self.message = message
        self.conflict = conflict
