"""数据分享业务规则（内部管理边界）：状态流转、字段校验与筛选口径都收在这里。

本模块是分享配置的唯一写入口，返回给管理接口的记录一律是深拷贝：
调用方（含路由层序列化之外的任何处理）改写返回对象都不会回写仓库记录。

公开读取不经过这里，而由 :mod:`app.services.public_share` 的只读边界负责；
每次状态流转都会通知公开边界推进授权版本，保证撤销立刻对后续读取生效。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.public_share import public_share_service
from app.store import store

MODULE = "share"
REQUIRED_FIELDS = ["链接编号", "目标数据集", "访问密码"]
STATUS_ORDER = ["草稿", "待生成", "已生效", "已失效"]
ACTION_RULES = {"提交生成": "待生成", "生效分享": "已生效", "撤销链接": "已失效"}
NEGATIVE_ACTIONS = ["撤销链接"]


class ShareService:
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
                rows = [row for row in rows if keyword in str(row.get("链接编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            page_rows = [deepcopy(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        # 管理接口需要完整配置（含访问密码），但拿到的是拷贝，不与仓库共享引用。
        return store.find_copy(MODULE, entry_id)

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
            # 新链接先建立授权版本基线；公开边界默认拒绝未生效链接。
            public_share_service.sync_authorization(entry["id"])
            return deepcopy(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"分享链接 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据分享可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 状态流转与授权版本推进在同一把锁内完成；撤销会同时作废公开投影缓存。
            public_share_service.sync_authorization(entry_id)
            return deepcopy(entry), f"分享链接已{action}"
