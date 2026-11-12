"""数据分享公开边界与内部管理边界的回归测试。

只用标准库（unittest + 线程），运行：

    cd backend && .venv/bin/python -m unittest app.tests.test_share_boundaries -v
"""
from __future__ import annotations

import threading
import unittest
from copy import deepcopy

from app.routers import public as public_router
from app.services.dataset_catalog import dataset_catalog
from app.services.datastore import DatastoreService
from app.services.public_share import (
    INTERNAL_LINK_FIELDS,
    SECRET_LINK_FIELDS,
    ProjectionCache,
    PublicShareService,
    ShareNotFound,
    SharePasswordInvalid,
    ShareTargetMissing,
)
from app.services.share import ShareService
from app.store import store

SHARE_MODULE = "share"
DATASTORE_MODULE = "datastore"

# 公开投影任何层级都不允许出现的键。
FORBIDDEN_KEYS = (
    SECRET_LINK_FIELDS
    | INTERNAL_LINK_FIELDS
    | {"负责人", "id", "status", "pending", "abnormal"}
)


def _flatten_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            found |= _flatten_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _flatten_keys(item)
    return found


class ShareBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从干净的种子数据开始，避免用例之间通过全局仓库串状态。
        store.reset()
        # 每个用例使用全新的公开服务与缓存，授权版本从基线重新计数。
        self.share_service = ShareService()
        self.datastore_service = DatastoreService()
        self.public_service = PublicShareService(catalog=dataset_catalog, cache=ProjectionCache())
        # 种子数据里 id=3 的链接为「已生效」，目标为「数据集管理样例3」，密码见种子。
        self.effective_share_id = 3
        self.effective_password = "数据分享样例3"
        self.target_name = "数据集管理样例3"

    # ---- 内部管理边界：响应契约保持不变，但返回对象与仓库解耦 ----

    def test_management_contract_keeps_full_configuration(self) -> None:
        entry = self.share_service.get_entry(self.effective_share_id)
        assert entry is not None
        # 既有契约：完整存储字典，含密码与内部簿记字段。
        for field_name in (
            "id",
            "status",
            "pending",
            "abnormal",
            "链接编号",
            "目标数据集",
            "访问密码",
            "有效期天数",
            "最大浏览次数",
            "下载开关",
            "水印内容",
            "过期自动清理",
        ):
            self.assertIn(field_name, entry)

    def test_mutating_management_response_does_not_touch_store(self) -> None:
        entry = self.share_service.get_entry(self.effective_share_id)
        assert entry is not None
        before = deepcopy(store.find(SHARE_MODULE, self.effective_share_id))
        entry["访问密码"] = "被外部改写的密码"
        entry["链接编号"] = "被外部改写的编号"
        entry["new_key"] = "value"
        after = store.find(SHARE_MODULE, self.effective_share_id)
        self.assertEqual(before, after)
        self.assertNotIn("new_key", after or {})

    def test_list_and_action_responses_are_isolated_copies(self) -> None:
        items, total = self.share_service.list_entries(page=1, size=10)
        self.assertGreater(total, 0)
        items[0]["访问密码"] = "改列表返回值"
        stored = store.find(SHARE_MODULE, int(items[0]["id"]))
        self.assertNotEqual(stored and stored["访问密码"], "改列表返回值")

        entry, message = self.share_service.run_action(3, "撤销链接")
        assert entry is not None
        entry["status"] = "被外部改写"
        stored = store.find(SHARE_MODULE, 3)
        self.assertEqual(stored and stored["status"], "已失效")
        self.assertIn("撤销链接", message)

    def test_dataset_definition_response_is_isolated_copy(self) -> None:
        definition = self.datastore_service.get_entry(3)
        assert definition is not None
        before = deepcopy(store.find(DATASTORE_MODULE, 3))
        definition["数据集名称"] = "外部改名"
        definition["来源表名"] = "外部改表"
        after = store.find(DATASTORE_MODULE, 3)
        self.assertEqual(before, after)

    # ---- 公开投影：白名单、无密码、无内部信息 ----

    def test_public_projection_excludes_secrets_and_internal_fields(self) -> None:
        view = self.public_service.open_share(self.effective_share_id, self.effective_password)
        payload = view.to_dict()
        leaked = _flatten_keys(payload) & FORBIDDEN_KEYS
        self.assertEqual(leaked, set(), f"公开投影泄漏内部字段：{leaked}")
        self.assertEqual(
            set(payload["分享链接"].keys()),
            {"链接编号", "目标数据集", "下载开关", "水印内容"},
        )

    def test_public_projection_carries_stable_dataset_reference(self) -> None:
        view = self.public_service.open_share(self.effective_share_id, self.effective_password)
        payload = view.to_dict()
        self.assertEqual(
            payload["数据集引用"],
            {"数据集编号": "DATA-0003", "数据集名称": self.target_name},
        )
        # 可分享定义只含白名单字段，且与内部完整定义不是同一个对象。
        internal = store.find(DATASTORE_MODULE, 3)
        self.assertNotIn("status", payload["数据集定义"])
        self.assertIn("数据集编号", payload["数据集定义"])
        self.assertIn("来源表名", payload["数据集定义"])
        self.assertIsNot(internal, payload["数据集定义"])

    def test_shareable_dataset_definition_endpoint_shape(self) -> None:
        shareable = self.datastore_service.shareable_definition(3)
        assert shareable is not None
        payload = shareable.to_dict()
        leaked = _flatten_keys(payload) & FORBIDDEN_KEYS
        self.assertEqual(leaked, set())
        # 投影是可变副本，改它不影响原始定义。
        payload["数据集名称"] = "改投影"
        internal = store.find(DATASTORE_MODULE, 3)
        self.assertEqual(internal and internal["数据集名称"], self.target_name)

    def test_name_based_and_code_based_links_both_resolve(self) -> None:
        # 既有按目标名称关联的链接（种子数据）正常解析。
        by_name = dataset_catalog.resolve_ref(self.target_name)
        self.assertIsNotNone(by_name)
        self.assertEqual(by_name and by_name.code, "DATA-0003")
        # 直接填写数据集编号的链接同样可解析。
        by_code = dataset_catalog.resolve_ref("DATA-0003")
        self.assertIsNotNone(by_code)
        self.assertEqual(by_code and by_code.name, self.target_name)
        self.assertIsNone(dataset_catalog.resolve_ref("不存在的数据集"))

    # ---- 授权裁决：未生效/撤销/密码错误全部服务端拒绝 ----

    def test_non_effective_and_missing_shares_are_rejected(self) -> None:
        # id=1 为草稿、id=2 为待生成，均不允许公开读取。
        for share_id in (1, 2):
            with self.assertRaises(ShareNotFound):
                self.public_service.open_share(share_id, "任意密码")
        with self.assertRaises(ShareNotFound):
            self.public_service.open_share(99999, "任意密码")

    def test_wrong_password_is_rejected_without_leaking_projection(self) -> None:
        with self.assertRaises(SharePasswordInvalid):
            self.public_service.open_share(self.effective_share_id, "错误密码")

    def test_revoked_share_rejects_subsequent_reads(self) -> None:
        # 撤销前可读。
        view = self.public_service.open_share(self.effective_share_id, self.effective_password)
        self.assertIn("分享链接", view.to_dict())
        # 通过内部管理边界撤销，并同步授权版本。
        self.public_service.sync_authorization(self.effective_share_id)
        with store.lock:
            row = store.find(SHARE_MODULE, self.effective_share_id)
            assert row is not None
            row["status"] = "已失效"
            row["pending"] = False
            row["abnormal"] = True
        # 撤销后即使密码正确也拒绝，且不是靠前端隐藏。
        with self.assertRaises(ShareNotFound):
            self.public_service.open_share(self.effective_share_id, self.effective_password)

    def test_dangling_target_is_rejected(self) -> None:
        # 新建一条指向不存在数据集的已生效链接。
        with store.lock:
            rows = store.rows(SHARE_MODULE)
            dangling_id = max(int(row["id"]) for row in rows) + 1
            rows.append({
                "id": dangling_id,
                "status": "已生效",
                "pending": False,
                "abnormal": False,
                "链接编号": "SHAR-DANGLING",
                "目标数据集": "已经下线的数据集",
                "访问密码": "pw",
            })
        with self.assertRaises(ShareTargetMissing):
            self.public_service.open_share(dangling_id, "pw")

    # ---- 缓存：不延长授权、不绕过密码、不共享可变对象 ----

    def test_cache_hit_still_requires_password_and_latest_authorization(self) -> None:
        first = self.public_service.open_share(
            self.effective_share_id, self.effective_password
        )
        second = self.public_service.open_share(
            self.effective_share_id, self.effective_password
        )
        # 同版本命中同一份权威缓存，但两次返回值彼此独立、且都不与缓存共享可变引用。
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first.link, second.link)
        self.assertIsNot(first.dataset, second.dataset)
        with self.assertRaises(SharePasswordInvalid):
            self.public_service.open_share(self.effective_share_id, "错误密码")

        # 状态流转推进授权版本，旧缓存不再服务任何请求。
        self.public_service.sync_authorization(self.effective_share_id)
        with store.lock:
            row = store.find(SHARE_MODULE, self.effective_share_id)
            assert row is not None
            row["status"] = "已失效"
        with self.assertRaises(ShareNotFound):
            self.public_service.open_share(self.effective_share_id, self.effective_password)

    def test_returned_view_mutation_does_not_poison_cache_or_store(self) -> None:
        view = self.public_service.open_share(
            self.effective_share_id, self.effective_password
        )
        view.link["链接编号"] = "篡改缓存"
        view.dataset["数据集名称"] = "篡改数据集"
        again = self.public_service.open_share(
            self.effective_share_id, self.effective_password
        )
        self.assertEqual(again.link["链接编号"], "SHAR-0003")
        self.assertEqual(again.dataset["数据集名称"], self.target_name)
        internal = store.find(DATASTORE_MODULE, 3)
        self.assertEqual(internal and internal["数据集名称"], self.target_name)

    def test_projection_generation_creates_no_links_or_definitions(self) -> None:
        share_count_before = len(store.rows(SHARE_MODULE))
        dataset_count_before = len(store.rows(DATASTORE_MODULE))
        self.public_service.open_share(self.effective_share_id, self.effective_password)
        self.public_service.open_share(self.effective_share_id, self.effective_password)
        self.assertEqual(len(store.rows(SHARE_MODULE)), share_count_before)
        self.assertEqual(len(store.rows(DATASTORE_MODULE)), dataset_count_before)

    # ---- 并发：撤销与读取遵守同一次授权裁决 ----

    def test_concurrent_revoke_and_read_never_mix_states(self) -> None:
        outcomes: list[object] = []
        barrier = threading.Barrier(7)  # 6 个读取线程 + 1 个撤销线程

        def reader() -> None:
            barrier.wait()
            try:
                payload = self.public_service.open_share(
                    self.effective_share_id, self.effective_password
                ).to_dict()
                # 成功时必须是完整且干净的投影，不能出现半撤销字段。
                self.assertNotIn("访问密码", _flatten_keys(payload))
                self.assertEqual(
                    payload["数据集引用"]["数据集名称"], self.target_name
                )
                outcomes.append(("ok", payload))
            except ShareNotFound:
                outcomes.append(("revoked", None))

        def revoker() -> None:
            barrier.wait()
            self.public_service.sync_authorization(self.effective_share_id)
            with store.lock:
                row = store.find(SHARE_MODULE, self.effective_share_id)
                assert row is not None
                row["status"] = "已失效"
                row["pending"] = False
                row["abnormal"] = True

        threads = [threading.Thread(target=reader) for _ in range(6)]
        threads.append(threading.Thread(target=revoker))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(outcomes), 6)
        self.assertTrue({kind for kind, _ in outcomes} <= {"ok", "revoked"})
        # 每个成功结果都是完整投影（引用 + 定义 + 链接三段齐备），不存在字段拼接。
        for kind, payload in outcomes:
            if kind == "ok":
                assert payload is not None
                self.assertEqual(set(payload.keys()), {"分享链接", "数据集引用", "数据集定义"})
        # 裁决落定后，后续读取必须全部拒绝。
        with self.assertRaises(ShareNotFound):
            self.public_service.open_share(self.effective_share_id, self.effective_password)

    # ---- 路由层：错误映射为 HTTP 状态码，成功体不含内部字段 ----

    def test_router_maps_authorization_failures(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            public_router.get_public_share(self.effective_share_id, "错误密码")
        self.assertEqual(ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as ctx:
            public_router.get_public_share(99999, "任意密码")
        self.assertEqual(ctx.exception.status_code, 404)

        payload = public_router.get_public_share(
            self.effective_share_id, self.effective_password
        )
        self.assertEqual(_flatten_keys(payload) & FORBIDDEN_KEYS, set())


if __name__ == "__main__":
    unittest.main()
