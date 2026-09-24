"""推送到网关 /v1/ingest/batch。

网关一条入口，双写企微 SCRM 线索池与 trade-crm —— 所以这里只打一个地方。
分数由网关算并在响应里返回，miner 不重复实现打分（重复必漂移）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter

from .records import CompanyRecord

# 与网关 maxLeadBatchItems 对齐。
BATCH_SIZE = 200


class PushError(RuntimeError):
    pass


def chunk(items: list, size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def push_batch(url: str, site_key: str, records: list[CompanyRecord],
               timeout: float = 60.0, resync: bool = False) -> dict:
    payload = {"items": [r.to_ingest_item() for r in records]}
    if resync:
        # 已存在的线索也重推一次 trade-crm。用于对方限流丢批后的补推 ——
        # 重放时 request_id 全是 duplicate，不开这个开关就永远补不上。
        payload["resync"] = True
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {site_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise PushError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PushError(f"网络错误: {exc}") from exc


def summarize(responses: list[dict]) -> dict:
    """汇总多批结果 + 分数分布。分数分布决定这批名单值不值得跟进。"""
    totals = Counter()
    bands = Counter()
    failures: list[dict] = []
    for resp in responses:
        for key in ("total", "created", "duplicate", "failed"):
            totals[key] += resp.get(key, 0)
        for item in resp.get("items", []):
            if band := item.get("score_band"):
                bands[band] += 1
            if item.get("status") == "failed":
                failures.append(item)
    return {"totals": dict(totals), "bands": dict(bands), "failures": failures}


def gateway_url() -> str:
    """默认走本地 ssh 隧道，不走公网。

    实测（2026-08-14）：网关的 /v1/ingest 只在内网 127.0.0.1:18090 上，
    公网 scrm.paibaowork.com/v1/ingest/* 被 nginx 挡在 405。这是对的设计 ——
    site_key 能写线索池，不该有公网入口。批量导入是运维动作，走隧道即可：

        ssh -N -L 18090:127.0.0.1:18090 aliyun
    """
    base = os.getenv("GATEWAY_URL", "http://127.0.0.1:18090").rstrip("/")
    return f"{base}/v1/ingest/batch"


def site_key() -> str:
    key = os.getenv("OUTBOUND_SITE_KEY", "").strip()
    if not key:
        raise SystemExit("缺少 OUTBOUND_SITE_KEY（网关 LEAD_SITE_KEYS_JSON 里 outbound site 的 key）")
    return key
