"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import query as router_query
from app.routers import chart as router_chart
from app.routers import source as router_source
from app.routers import aggregate as router_aggregate
from app.routers import filter as router_filter
from app.routers import reportgen as router_reportgen
from app.routers import anomaly as router_anomaly
from app.routers import drill as router_drill
from app.routers import compare as router_compare
from app.routers import pipeline as router_pipeline
from app.routers import datastore as router_datastore
from app.routers import forecast as router_forecast
from app.routers import segment as router_segment
from app.routers import scheduler as router_scheduler
from app.routers import notify as router_notify
from app.routers import cache as router_cache
from app.routers import tag as router_tag
from app.routers import share as router_share
from app.routers import public as router_public

ROUTERS = [router_query, router_chart, router_source, router_aggregate, router_filter, router_reportgen, router_anomaly, router_drill, router_compare, router_pipeline, router_datastore, router_forecast, router_segment, router_scheduler, router_notify, router_cache, router_tag, router_share, router_public]
