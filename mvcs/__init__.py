"""mvcs - 本地轻量级版本控制系统。

所有数据（对象、索引、引用、配置）只保存在工程目录下的 `.mvcs/` 隐藏目录中，
不调用 Git、不访问远程仓库、不使用数据库或任何外部服务。
"""

from .repo import Repository, MvcsError, CheckoutConflict, MergeConflict, find_repo

__all__ = ["Repository", "MvcsError", "CheckoutConflict", "MergeConflict", "find_repo"]

__version__ = "0.1.0"
