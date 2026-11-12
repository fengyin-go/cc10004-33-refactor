"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

并发约定：所有「读-判-写」需要保持同一次裁决一致性的流程，都必须在 ``store.lock``
这把可重入锁内完成；对外返回记录时统一走 :meth:`Store.find_copy` 之类的拷贝接口，
避免调用方拿着内部字典的引用间接改写原记录。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 可重入锁：公开投影在临界区内还会经由目录服务再次读取同一把锁。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    @property
    def lock(self) -> threading.RLock:
        """仓库级裁决锁：授权裁决、状态流转等需要原子视角的流程共用。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        """返回活动表（内部使用）：调用方不得把表内字典直接泄漏到服务边界之外。"""
        return self._tables.setdefault(module, [])

    def reset(self) -> None:
        """恢复到种子数据：供测试用例在干净状态下重复运行。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """按主键定位活动记录，返回内部字典本身；只允许在锁内做读写使用。"""
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_copy(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """对外读取入口：返回深拷贝，改写返回对象不会回写到仓库。"""
        with self._lock:
            row = self.find(module, entry_id)
            return deepcopy(row) if row is not None else None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        with self._lock:
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
