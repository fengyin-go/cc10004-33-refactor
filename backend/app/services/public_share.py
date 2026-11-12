"""数据分享的公开只读边界。

与 :mod:`app.services.share` 的内部管理边界严格分离：

* 只接受能力凭证（链接主键 + 访问密码），不提供任何登记、流转、列表入口；
* 输出字段走白名单投影，访问密码、内部簿记字段、负责人等内部信息永不外泄；
* 撤销（以及任何状态流转）之后再次读取一律拒绝，裁决在服务端完成，不依赖前端隐藏；
* 「查找链接 → 状态裁决 → 密码校验 → 生成投影」在同一次仓库锁裁决内完成，
  并发撤销与读取不会拼接出撤销前后混合的字段；
* 投影缓存按授权版本号（revision）区分，链接每次状态流转都会推进版本并作废旧缓存，
  因此缓存命中不会延长、也不会恢复任何授权。
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.services.dataset_catalog import (
    INTERNAL_ONLY_DATASET_FIELDS,
    DatasetCatalog,
    dataset_catalog,
)
from app.store import store

SHARE_MODULE = "share"
EFFECTIVE_STATUS = "已生效"

# 外部展示字段：链接的可公开业务信息（白名单）。
PUBLIC_LINK_FIELDS: tuple[str, ...] = (
    "链接编号",
    "目标数据集",
    "下载开关",
    "水印内容",
)

# 访问凭证：仅用于裁决，绝不进入任何对外投影。
SECRET_LINK_FIELDS: frozenset[str] = frozenset({"访问密码"})

# 内部管理字段：运营策略与内部簿记，仅属于内部编辑边界。
INTERNAL_LINK_FIELDS: frozenset[str] = frozenset(
    {
        "有效期天数",
        "最大浏览次数",
        "过期自动清理",
        "id",
        "status",
        "pending",
        "abnormal",
    }
)

_FORBIDDEN_KEYS: frozenset[str] = (
    SECRET_LINK_FIELDS | INTERNAL_LINK_FIELDS | INTERNAL_ONLY_DATASET_FIELDS
)


class ShareAccessError(Exception):
    """公开访问被拒绝的基类；``status_code`` 供路由层映射。"""

    status_code = 404


class ShareNotFound(ShareAccessError):
    """链接不存在、未生效或已撤销：统一按不可发现处理，避免枚举有效链接。"""

    status_code = 404


class SharePasswordInvalid(ShareAccessError):
    """链接有效但访问密码不匹配。"""

    status_code = 403


class ShareTargetMissing(ShareAccessError):
    """链接有效但目标数据集定义已不存在，无法生成投影。"""

    status_code = 409


@dataclass(frozen=True)
class PublicShareView:
    """一次授权裁决产出的不可变公开视图。"""

    revision: int
    link: dict[str, Any]
    dataset_ref: dict[str, str]
    dataset: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # revision 是内部缓存键，不属于对外契约。
        return {
            "分享链接": dict(self.link),
            "数据集引用": dict(self.dataset_ref),
            "数据集定义": dict(self.dataset),
        }


class ProjectionCache:
    """授权版本感知的投影缓存。

    缓存项以 ``(链接主键, 授权版本号)`` 为键：链接一旦发生状态流转，版本号推进，
    旧键立刻失效；主动撤销时还会把该链接的历史缓存全部剔除。
    """

    def __init__(self, max_size: int = 256) -> None:
        self._max_size = max_size
        self._items: "OrderedDict[tuple[int, int], PublicShareView]" = OrderedDict()

    def get(self, entry_id: int, revision: int) -> PublicShareView | None:
        cached = self._items.get((entry_id, revision))
        if cached is None:
            return None
        self._items.move_to_end((entry_id, revision))
        return cached

    def put(self, entry_id: int, view: PublicShareView) -> None:
        key = (entry_id, view.revision)
        self._items[key] = view
        self._items.move_to_end(key)
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)

    def invalidate(self, entry_id: int) -> None:
        for stale_key in [key for key in self._items if key[0] == entry_id]:
            self._items.pop(stale_key, None)

    def clear(self) -> None:
        self._items.clear()


class PublicShareService:
    """公开只读服务：只做裁决与投影，不持有任何可写回仓库的引用。"""

    def __init__(
        self,
        catalog: DatasetCatalog | None = None,
        cache: ProjectionCache | None = None,
    ) -> None:
        self._catalog = catalog or dataset_catalog
        self._cache = cache or ProjectionCache()
        # 每个链接当前的授权版本号；状态流转由内部管理边界通知本服务推进。
        self._revisions: dict[int, int] = {}

    def sync_authorization(self, entry_id: int) -> None:
        """内部管理边界在状态流转后调用：推进授权版本并作废该链接的旧投影。"""
        with store.lock:
            self._revisions[entry_id] = self._revisions.get(entry_id, 0) + 1
            self._cache.invalidate(entry_id)

    def open_share(self, entry_id: int, password: str) -> PublicShareView:
        """凭链接主键与访问密码读取公开投影。

        整个裁决在同一把仓库锁内完成，保证并发撤销时看到的要么是裁决前的完整视图、
        要么是拒绝，绝不会出现字段拼接。
        """
        with store.lock:
            entry = store.find(SHARE_MODULE, entry_id)
            if entry is None or entry.get("status") != EFFECTIVE_STATUS:
                raise ShareNotFound("分享链接不存在、未生效或已撤销")
            if str(entry.get("访问密码", "")) != str(password or ""):
                raise SharePasswordInvalid("访问密码不正确")

            revision = self._revisions.get(entry_id, 0)
            cached = self._cache.get(entry_id, revision)
            if cached is not None:
                # 缓存只保存权威视图；对外返回独立拷贝，调用方篡改无法污染缓存。
                return deepcopy(cached)

            view = self._project(entry_id, entry, revision)
            self._cache.put(entry_id, view)
            # 首次返回同样给出拷贝，保证「缓存命中与否」对外行为一致。
            return deepcopy(view)

    def _project(
        self, entry_id: int, entry: dict[str, Any], revision: int
    ) -> PublicShareView:
        """根据白名单构造不可变公开投影；不读取、不回写任何内部字段。"""
        link_payload = {
            field: entry[field]
            for field in PUBLIC_LINK_FIELDS
            if field in entry and field not in _FORBIDDEN_KEYS
        }

        target = str(entry.get("目标数据集", "")).strip()
        ref = self._catalog.resolve_ref(target)
        if ref is None:
            raise ShareTargetMissing(f"目标数据集「{target}」不存在，无法生成公开投影")
        shareable = self._catalog.shareable_definition(target)
        if shareable is None:  # 理论上与 ref 一致，属于防御性分支。
            raise ShareTargetMissing(f"目标数据集「{target}」不存在，无法生成公开投影")

        view = PublicShareView(
            revision=revision,
            link=link_payload,
            dataset_ref=ref.to_dict(),
            dataset=shareable.to_dict(),
        )
        self._assert_clean(view)
        return view

    @staticmethod
    def _assert_clean(view: PublicShareView) -> None:
        """出口防线：递归确认投影里没有混入任何密码或内部字段。"""

        def scan(payload: Any) -> None:
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key in _FORBIDDEN_KEYS:
                        raise AssertionError(f"公开投影混入内部字段：{key}")
                    scan(value)
            elif isinstance(payload, list):
                for item in payload:
                    scan(item)

        scan(view.to_dict())


public_share_service = PublicShareService()
