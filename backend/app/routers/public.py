"""公开只读接口：数据分享的外部访问边界。

与内部管理接口（``/api/share``、``/api/datastore``）完全分离：

* 只凭能力凭证（分享链接编号 + 访问密码）读取，不提供登记、流转、列表入口；
* 撤销、未生效或不存在的链接在服务端直接拒绝，不依赖前端隐藏字段；
* 响应只包含白名单投影，访问密码与内部负责人等信息不会出现在任何层级。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.public_share import (
    ShareAccessError,
    public_share_service,
)

router = APIRouter(prefix="/api/public", tags=["公开分享"])


@router.get("/shares/{entry_id}", response_model=dict)
def get_public_share(
    entry_id: int,
    password: str = Query(..., description="分享链接的访问密码"),
) -> dict:
    """按授权裁决返回分享链接的公开投影；裁决失败时给出不泄漏链接状态的错误。"""
    try:
        view = public_share_service.open_share(entry_id, password)
    except ShareAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return view.to_dict()
