"""数据集管理业务规则（内部编辑边界）：状态流转、字段校验与筛选口径都收在这里。

对外只返回记录的深拷贝，避免调用方通过修改返回对象间接改写原始定义。
需要把数据集暴露给数据分享等外部场景时，统一走 :meth:`shareable_definition`
返回的只读可分享投影（由共享目录 :mod:`app.services.dataset_catalog` 生成），
该投影按字段白名单构造，不含任何内部管理字段。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.dataset_catalog import ShareableDataset, dataset_catalog
from app.store import store

MODULE = "datastore"
REQUIRED_FIELDS = ["数据集编号", "数据集名称", "来源表名"]
STATUS_ORDER = ["草稿", "待审核", "已生效", "已废弃"]
ACTION_RULES = {"提交审核": "待审核", "生效发布": "已生效", "废弃下线": "已废弃"}
NEGATIVE_ACTIONS = []


class DatastoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("数据集编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            page_rows = [deepcopy(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        # 完整内部定义（含内部簿记字段），但返回拷贝，仓库记录不被外部共享。
        return store.find_copy(MODULE, entry_id)

    def shareable_definition(self, entry_id: int) -> ShareableDataset | None:
        """只读的可分享定义：字段白名单投影，修改它不影响原始定义。"""
        return dataset_catalog.shareable_definition_by_id(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return deepcopy(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"数据集定义 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据集管理可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return deepcopy(entry), f"数据集定义已{action}"
