"""工商信息补全。

**为什么是配置驱动而不是写死爱企查的端点**：这套代码写作时无法核实爱企查开放平台
的真实端点、鉴权方式与响应字段，猜一个写死进核心链路，等于给自己埋一个只有上线才
会炸的坑。所以这里定义一个通用 HTTP provider，端点/鉴权/字段映射全部从环境变量注入
——拿到真实凭据后填 env 即可，不用改代码。

补全的核心目的只有两个：
  1. 拿到统一社会信用代码（稳定身份，跨源去重靠它）
  2. 判断经营范围里有没有进出口经营权（外贸企业最强过滤器，ICP 打分权重 0.35）
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .records import CompanyRecord, normalize_phone, normalize_website

# 经营范围里出现这些词 = 有进出口业务资质。
IMPORT_EXPORT_MARKERS = (
    "进出口",
    "货物进出口",
    "技术进出口",
    "对外贸易",
    "国际贸易",
    "出口",
)


class Enricher(Protocol):
    def enrich(self, record: CompanyRecord) -> CompanyRecord: ...


class NoopEnricher:
    """不补全，只用名录自带字段。没有凭据时的默认，链路照样跑通。"""

    def enrich(self, record: CompanyRecord) -> CompanyRecord:
        return record


class HttpEnricher:
    """通用工商数据 provider。

    env：
      ENRICH_API_URL       查询端点，用 {name} 占位公司名，如 https://host/path?keyword={name}
      ENRICH_API_KEY       鉴权值
      ENRICH_AUTH_HEADER   鉴权头名，默认 Authorization
      ENRICH_AUTH_PREFIX   鉴权值前缀，默认空（需要 Bearer 就填 "Bearer "）
      ENRICH_FIELD_MAP     JSON，把响应字段映射到我们的字段，支持点号路径。例：
                           {"credit_code":"data.creditCode","scope":"data.scope",
                            "staff_size":"data.employeeCount","phone":"data.telephone"}
    """

    def __init__(self, url: str, key: str, field_map: dict[str, str],
                 auth_header: str = "Authorization", auth_prefix: str = "",
                 timeout: float = 10.0) -> None:
        self.url = url
        self.key = key
        self.field_map = field_map
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "HttpEnricher | None":
        url = os.getenv("ENRICH_API_URL", "").strip()
        key = os.getenv("ENRICH_API_KEY", "").strip()
        if not url or not key:
            return None
        try:
            field_map = json.loads(os.getenv("ENRICH_FIELD_MAP", "{}"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"ENRICH_FIELD_MAP 不是合法 JSON: {exc}") from exc
        return cls(
            url=url,
            key=key,
            field_map=field_map,
            auth_header=os.getenv("ENRICH_AUTH_HEADER", "Authorization"),
            auth_prefix=os.getenv("ENRICH_AUTH_PREFIX", ""),
        )

    def enrich(self, record: CompanyRecord) -> CompanyRecord:
        payload = self._query(record.company)
        if payload is None:
            return record
        return apply_enrichment(record, payload, self.field_map)

    def _query(self, company: str) -> dict | None:
        url = self.url.replace("{name}", urllib.parse.quote(company))
        req = urllib.request.Request(url, headers={
            self.auth_header: f"{self.auth_prefix}{self.key}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            # 补全失败不该丢线索：名录自带的联系方式仍然可用，只是分数低一档。
            print(f"  enrich 失败（保留原始记录）: {company}: {exc}")
            return None


def dig(payload: dict, path: str):
    """按点号路径取值，任一段缺失返回 None。"""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def has_import_export(scope: str) -> bool:
    return any(marker in scope for marker in IMPORT_EXPORT_MARKERS)


def parse_staff_size(raw) -> int:
    """人数字段可能是 int、"50人"、"50-99人" 这类区间。区间取下界（保守）。"""
    if isinstance(raw, (int, float)):
        return int(raw)
    if not isinstance(raw, str):
        return 0
    numbers = re.findall(r"\d+", raw)
    return int(numbers[0]) if numbers else 0


def apply_enrichment(record: CompanyRecord, payload: dict, field_map: dict[str, str]) -> CompanyRecord:
    """把 provider 响应合并进记录。纯函数，便于单测。

    只填空缺字段 —— 名录自带的联系方式通常比第三方库里的更新。
    """
    def value_for(key: str):
        path = field_map.get(key)
        return dig(payload, path) if path else None

    if not record.credit_code and (code := value_for("credit_code")):
        record.credit_code = str(code).strip()
    if scope := value_for("scope"):
        record.import_export = has_import_export(str(scope))
        if not record.industry:
            record.industry = str(scope)[:60]
    if not record.staff_size and (size := value_for("staff_size")) is not None:
        record.staff_size = parse_staff_size(size)
    if not record.phone and (phone := value_for("phone")):
        record.phone = normalize_phone(str(phone))
    if not record.website and (site := value_for("website")):
        record.website = normalize_website(str(site))
    if not record.province and (province := value_for("province")):
        record.province = str(province).strip()
    return record


def build_enricher() -> Enricher:
    return HttpEnricher.from_env() or NoopEnricher()
