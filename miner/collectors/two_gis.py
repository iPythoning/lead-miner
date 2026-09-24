"""2GIS Catalog API 采集 —— 中亚 ABM 买家池(FDE 交付物侧,ADR-0001 D2 右半边)。

**信号含义**:2GIS 是俄语区/中亚覆盖最强的商户目录(阿拉木图单城「грузовые
автомобили」类目实测 440+ 家)。在 2GIS 登记「卡车销售/半挂车/货运」类目的
哈萨克/乌兹别克公司 = 正在经营商用车生意的潜在进口买家 —— 这是给梁山半挂车
客户建 ABM 池的底库,与海关镜像数据交叉验证(2GIS 定「谁」,贸易数据定「量与真伪」)。

---

⚠️ **端点与字段基于既有知识,未经本机核实** —— 先 `2gis-probe` 再 `2gis-fetch`
(与 meta_ads 同哲学):probe 用最小请求探明真实响应结构与 key 权限,没过就
批量拉取只会撞墙。API key 在 dev.2gis.com 免费注册,env `TWOGIS_API_KEY` 注入。

**合规**(docs/sources.md 红线):只取商户在目录里**主动公示**的企业联系方式
(名称/电话/网站/类目/城市),不碰任何个人数据;官方 API 自带频控,再加
请求间隔;产出 CSV 走 `miner import` 正门,去重与打分都在下游。

**产出可直接 import**:与 Meta 广告库不同,2GIS 商户自带公示电话,
CSV 列名对齐 `records.COLUMN_ALIASES`,无需 wigolo 补全即可
`python3 -m miner import <csv> --source 2gis-kz-truck --signal-date <date>`。
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CATALOG_ENDPOINT = os.getenv("TWOGIS_CATALOG_ENDPOINT",
                             "https://catalog.api.2gis.com/3.0/items")
REGION_ENDPOINT = os.getenv("TWOGIS_REGION_ENDPOINT",
                            "https://catalog.api.2gis.com/2.0/region/search")

# 商用车垂直的俄语检索词:卖车的(经销商)+ 用车的(物流/工程 = 终端买家)。
DEFAULT_QUERIES = [
    "продажа грузовых автомобилей",   # 卡车销售
    "полуприцепы",                    # 半挂车
    "спецтехника продажа",            # 工程机械销售
    "грузоперевозки",                 # 货运公司(整车终端买家)
]

# 每请求最多 50 条是目录类 API 的常见上限;真实上限以 probe 为准。
PAGE_SIZE = int(os.getenv("TWOGIS_PAGE_SIZE", "50"))

ITEM_FIELDS = "items.contact_groups,items.adm_div,items.rubrics,items.point"


class TwoGisError(RuntimeError):
    """带上 2GIS 原始错误体 —— meta.error 通常直接说明 key/参数问题,别吞掉。"""


def _get(url: str, params: dict[str, str]) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}",
                                 headers={"User-Agent": "paibao-lead-miner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise TwoGisError(f"HTTP {exc.code}: {body}") from exc


def require_key() -> str:
    key = os.getenv("TWOGIS_API_KEY", "").strip()
    if not key:
        raise TwoGisError("缺 TWOGIS_API_KEY —— dev.2gis.com 免费注册,拿到后 export 再跑")
    return key


def find_region(key: str, city_query: str) -> dict:
    """城市名 → region(id + 名称)。返回首个命中,probe 时人工确认。"""
    data = _get(REGION_ENDPOINT, {"q": city_query, "key": key})
    items = (data.get("result") or {}).get("items") or []
    if not items:
        raise TwoGisError(f"region 查询无结果: {city_query!r},原始响应 meta={data.get('meta')}")
    return items[0]


def pick_contact(contact_groups: list | None, kind: str) -> str:
    """从 contact_groups 提取首个指定类型联系方式。电话取 value(规范格式)优先。"""
    for group in contact_groups or []:
        for c in group.get("contacts") or []:
            if c.get("type") == kind:
                return (c.get("value") or c.get("text") or "").strip()
    return ""


def item_to_row(item: dict, *, country: str) -> dict[str, str]:
    """2GIS item → miner import 认识的 CSV 行(列名对齐 COLUMN_ALIASES)。"""
    adm = [a.get("name", "") for a in item.get("adm_div") or [] if a.get("name")]
    rubrics = [r.get("name", "") for r in item.get("rubrics") or [] if r.get("name")]
    cg = item.get("contact_groups")
    return {
        "company": (item.get("name") or "").strip(),
        "phone": pick_contact(cg, "phone"),
        "email": pick_contact(cg, "email"),
        "website": pick_contact(cg, "website"),
        # province 列承载「国家-城市」:下游没有 country 语义列,拼一起最省事。
        "province": f"{country}-{adm[-1]}" if adm else country,
        "industry": " / ".join(rubrics),
    }


def dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """按 (公司名, 电话) 去重 —— 多分店同号只留一条,批内幂等由下游网关再兜一层。"""
    seen: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        k = (r["company"].lower(), r["phone"])
        if r["company"] and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def probe(key: str, city_query: str) -> dict:
    """最小探测:region + 一页 items,原样返回供人工确认结构再批量拉。"""
    region = find_region(key, city_query)
    page = _get(CATALOG_ENDPOINT, {
        "q": DEFAULT_QUERIES[0], "region_id": str(region.get("id", "")),
        "page": "1", "page_size": "3", "fields": ITEM_FIELDS, "key": key,
    })
    return {"region": region, "sample": page}


def fetch(key: str, region_id: str, queries: list[str], *, country: str,
          max_pages: int = 10, sleep_s: float = 1.2) -> list[dict[str, str]]:
    """分页拉取多检索词 → 去重行。sleep 是合规红线的「低频」,别调成 0。"""
    rows: list[dict[str, str]] = []
    for q in queries:
        for page in range(1, max_pages + 1):
            data = _get(CATALOG_ENDPOINT, {
                "q": q, "region_id": region_id, "page": str(page),
                "page_size": str(PAGE_SIZE), "fields": ITEM_FIELDS, "key": key,
            })
            items = (data.get("result") or {}).get("items") or []
            if not items:
                break
            rows.extend(item_to_row(i, country=country) for i in items)
            time.sleep(sleep_s)
    return dedupe(rows)


def write_csv(rows: list[dict[str, str]], path: str) -> int:
    cols = ["company", "phone", "email", "website", "province", "industry"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
