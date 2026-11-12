"""跨模块的稳定数据集引用与只读可分享定义。

数据集管理（``datastore``）与数据分享（``share``）是两个独立模块，历史上分享链接
只用「目标数据集」的名称字符串做关联。这里把双方共用的引用收拢成只读值对象：

* :class:`DatasetRef`：跨模块稳定引用，业务键为「数据集编号 + 数据集名称」，
  解析时同时兼容按名称与按编号关联的历史链接；
* :class:`ShareableDataset`：数据集定义的只读可分享投影，字段走白名单，
  内部管理字段一律不进入投影。

本模块只做读取与投影，不修改任何原始定义，也不会创建新的分享链接。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.store import store

DATASTORE_MODULE = "datastore"

# 可对外分享的稳定字段（白名单）；其余字段一律视为内部配置。
SHAREABLE_DATASET_FIELDS: tuple[str, ...] = (
    "数据集编号",
    "数据集名称",
    "来源表名",
    "字段列表",
    "更新策略",
    "数据采样比例",
    "生命周期天数",
    "缓存开关",
)

# 内部字段：即使数据集定义以后新增这类字段，也不允许出现在公开投影里。
INTERNAL_ONLY_DATASET_FIELDS: frozenset[str] = frozenset({"负责人"})

# 数据集管理自身的内部簿记字段，不属于业务定义。
_DATASET_BOOKKEEPING_FIELDS: frozenset[str] = frozenset(
    {"id", "status", "pending", "abnormal"}
)


@dataclass(frozen=True)
class DatasetRef:
    """跨模块稳定数据集引用：只暴露业务键，不携带存储主键与内部状态。"""

    code: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"数据集编号": self.code, "数据集名称": self.name}


@dataclass(frozen=True)
class ShareableDataset:
    """数据集定义的只读可分享视图（外部展示边界）。"""

    ref: DatasetRef
    fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.fields)
        payload["数据集编号"] = self.ref.code
        payload["数据集名称"] = self.ref.name
        return payload


def _is_internal_field(field_name: str) -> bool:
    return (
        field_name in _DATASET_BOOKKEEPING_FIELDS
        or field_name in INTERNAL_ONLY_DATASET_FIELDS
    )


class DatasetCatalog:
    """数据集目录：供内部管理与公开投影共用的只读解析入口。"""

    def resolve_ref(self, target: str) -> DatasetRef | None:
        """把分享链接里的「目标数据集」解析成稳定引用。

        历史链接按数据集名称关联，因此名称优先；同时兼容直接填写数据集编号的链接。
        解析全程只读取快照，不在仓库里写入任何内容。
        """
        target = (target or "").strip()
        if not target:
            return None
        with store.lock:
            fallback: DatasetRef | None = None
            for row in store.rows(DATASTORE_MODULE):
                name = str(row.get("数据集名称", "")).strip()
                code = str(row.get("数据集编号", "")).strip()
                if name and name == target:
                    return DatasetRef(code=code, name=name)
                if code and code == target:
                    fallback = DatasetRef(code=code, name=name)
            return fallback

    def _project(self, row: dict[str, Any]) -> ShareableDataset:
        """根据白名单从原始定义生成只读投影；不接触原始字典的可变引用。"""
        ref = DatasetRef(
            code=str(row.get("数据集编号", "")),
            name=str(row.get("数据集名称", "")),
        )
        fields = {
            field: row[field]
            for field in SHAREABLE_DATASET_FIELDS
            if field in row and not _is_internal_field(field)
        }
        return ShareableDataset(ref=ref, fields=fields)

    def shareable_definition(self, target: str) -> ShareableDataset | None:
        """按名称/编号取得只读可分享定义；目标不存在时返回 ``None``。"""
        target = (target or "").strip()
        if not target:
            return None
        with store.lock:
            for row in store.rows(DATASTORE_MODULE):
                name = str(row.get("数据集名称", "")).strip()
                code = str(row.get("数据集编号", "")).strip()
                if (name and name == target) or (code and code == target):
                    return self._project(row)
        return None

    def shareable_definition_by_id(self, entry_id: int) -> ShareableDataset | None:
        """按数据集管理主键取得只读可分享定义（内部管理接口使用）。"""
        with store.lock:
            row = store.find(DATASTORE_MODULE, entry_id)
            return self._project(row) if row is not None else None


dataset_catalog = DatasetCatalog()
