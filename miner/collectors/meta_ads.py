"""Meta 广告库（Ad Library）采集。

**信号含义**：在投英文/海外广告的中国公司 = 已经在为海外获客掏现金，
付费意愿比"参展"更硬。且这是唯一免费、官方、结构化的强信号源。

---

⚠️ **端点与字段未经核实**（写作环境无网络）。因此本模块的正确用法是
**先 probe 再 fetch**：`probe()` 用最小请求探明真实响应结构、可用字段与权限边界，
拿到结果再决定过滤逻辑。probe 没过就批量拉取只会拉回一堆空数据或撞权限墙。

已知的关键不确定性（probe 就是用来消解它们的）：

1. **非政治广告的可见范围受限**。`ad_type=ALL` 在欧盟因 DSA 要求而开放，
   其他地区可能只返回政治/社会议题广告。故默认按「广告触达国家 = 欧盟主要市场」
   查询 —— 中国外贸企业投海外广告，目标市场通常覆盖 DE/FR/IT/ES/NL，
   能从这个口子捞到。
2. **返回里没有「广告主所在国」的可靠字段**，"是不是中国公司"只能靠落地页域名
   与页面名推断，本模块不做强断言，把判断材料原样带出去给人看。
3. 字段名随 API 版本变化，`DEFAULT_FIELDS` 可被 `META_AD_FIELDS` 覆盖。

---

**产出的是半成品，不是可入库线索**：广告库给得到公司名与落地页域名，
**给不到电话**，而 trade-crm 的 `Contact.phoneNumber` 是必填且唯一键。
所以本模块导出 CSV → 交给 wigolo 抓这些域名的 contact 页补联系方式
→ 再走 `miner import`。抓网页不在本仓职责内（见 AGENTS.md 边界）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
ENDPOINT = f"https://graph.facebook.com/{GRAPH_VERSION}/ads_archive"

# 欧盟主要市场：DSA 让这里的非政治广告可查，且是中国外贸企业的主力目标市场。
DEFAULT_COUNTRIES = ["DE", "FR", "IT", "ES", "NL"]

DEFAULT_FIELDS = [
    "id",
    "page_id",
    "page_name",
    "ad_creative_link_captions",
    "ad_creative_link_titles",
    "ad_creative_bodies",
    "ad_delivery_start_time",
    "publisher_platforms",
    "languages",
]


class MetaError(RuntimeError):
    """带上 Meta 原始错误体 —— 它通常直接指出是哪个参数/权限不对，别吞掉。"""


def access_token() -> str:
    raw = os.getenv("META_ACCESS_TOKEN", "")
    if not raw.strip():
        raise SystemExit(
            "缺少 META_ACCESS_TOKEN。需要 Meta 开发者账号并完成身份验证后签发的 token。\n"
            "token 只从环境变量读，不写进代码、日志或导出的 CSV。"
        )
    if problem := token_shape_problem(raw):
        raise SystemExit(
            f"META_ACCESS_TOKEN 形状可疑：{problem}\n"
            "先本地排掉复制问题，再谈权限 —— Meta 对格式坏掉的 token 只回一句 "
            "`Cannot parse access token`，看不出是复制错了还是拿错了东西。"
        )
    return raw.strip()


def token_shape_problem(raw: str) -> str:
    """本地预检 token 形状，在发请求前抓掉最常见的复制事故。

    Meta 对格式坏掉的 token 一律回 `Cannot parse access token`（code 190），
    这句话既不区分「复制时混进引号」也不区分「你拿的根本是 App Secret」，
    自己先查一遍比对着那句话猜快得多。

    返回问题描述；没问题返回空串。**不打印 token 本身**。
    """
    token = raw.strip()
    if token != raw.strip(" \t\r\n"):
        return "首尾有空白字符"
    if token[:1] in "\"'" or token[-1:] in "\"'":
        return "首尾带引号 —— export 时不要连引号一起复制进值里"
    # Bearer 判断必须在空格判断之前 —— 前缀自带空格，否则永远被泛化的空格提示吃掉。
    if token.lower().startswith("bearer "):
        return "带了 `Bearer ` 前缀 —— 这里只要 token 本身，前缀由程序加"
    if any(c in token for c in " \t\r\n"):
        return "中间含空格或换行 —— 多半是复制时断行了，token 应该是连续一串"
    if len(token) < 50:
        return (f"只有 {len(token)} 个字符，太短。User Access Token 通常 150+ 字符；"
                "这个长度更像 App ID 或 App Secret —— 那两个都不能当 access token 用")
    return ""


def _graph(path: str, params: dict, timeout: float = 30.0) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:800]
        raise MetaError(f"HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MetaError(f"网络错误: {exc}") from exc


def _call(params: dict, timeout: float = 30.0) -> dict:
    return _graph("ads_archive", params, timeout)


def diagnose_token() -> dict:
    """token 自检：它是谁的、授了哪些权限、能不能查广告库。

    回答「我这个 token 是不是广告的」——不靠翻文档，靠实际调用。
    每项独立捕获异常：某一项失败不影响其余诊断结果。
    """
    token = access_token()
    result: dict = {}

    try:
        result["identity"] = _graph("me", {"access_token": token, "fields": "id,name"})
    except MetaError as exc:
        result["identity_error"] = str(exc)

    try:
        payload = _graph("me/permissions", {"access_token": token})
        granted = [p["permission"] for p in payload.get("data", []) if p.get("status") == "granted"]
        result["granted_scopes"] = sorted(granted)
    except MetaError as exc:
        result["permissions_error"] = str(exc)

    # 终极判据：能不能真的读到广告库。Ad Library 的门槛是「账号完成身份验证」，
    # 而不是某个 ads_* scope —— 所以 scope 齐全也可能读不了，必须实测。
    try:
        probe()
        result["ad_library"] = "OK"
    except MetaError as exc:
        result["ad_library"] = "FAILED"
        result["ad_library_error"] = str(exc)

    return result


def probe(countries: list[str] | None = None) -> dict:
    """最小请求，探明响应结构、可用字段与权限边界。批量拉取前必跑。

    返回原始 JSON。失败时抛 MetaError 并带上 Meta 的原始错误体 —— 那段话
    通常明确指出是字段名不对、权限不足，还是该地区不可查。
    """
    return _call({
        "access_token": access_token(),
        "ad_reached_countries": json.dumps(countries or DEFAULT_COUNTRIES),
        "ad_type": "ALL",
        "ad_active_status": "ACTIVE",
        "fields": ",".join(DEFAULT_FIELDS),
        "limit": 1,
    })


def fetch_ads(countries: list[str], search_terms: str = "", max_pages: int = 5,
              page_size: int = 100, fields: list[str] | None = None) -> list[dict]:
    """分页拉取广告。max_pages 是硬上限，避免第一次跑就撞配额。"""
    params = {
        "access_token": access_token(),
        "ad_reached_countries": json.dumps(countries),
        "ad_type": "ALL",
        "ad_active_status": "ACTIVE",
        "fields": ",".join(fields or os.getenv("META_AD_FIELDS", "").split(",") or DEFAULT_FIELDS),
        "limit": page_size,
    }
    if search_terms:
        params["search_terms"] = search_terms

    ads, after, pages = [], None, 0
    while pages < max_pages:
        if after:
            params["after"] = after
        payload = _call(params)
        batch = payload.get("data", [])
        ads.extend(batch)
        after = payload.get("paging", {}).get("cursors", {}).get("after")
        pages += 1
        if not batch or not after:
            break
    return ads


def extract_domain(ad: dict) -> str:
    """从广告创意里找落地页域名 —— 这是后续反查官网联系方式的钥匙。"""
    for key in ("ad_creative_link_captions", "ad_creative_link_titles"):
        for value in ad.get(key) or []:
            value = (value or "").strip()
            if not value:
                continue
            candidate = value if value.startswith(("http://", "https://")) else "http://" + value
            host = urlparse(candidate).netloc.lower()
            # link_captions 常直接就是显示域名（example.com），但也可能是标题文案，
            # 用「含点且无空格」粗筛掉后者。
            if host and "." in host and " " not in host:
                return host.removeprefix("www.")
    return ""


def ad_to_row(ad: dict) -> dict:
    """广告 → CSV 行。列名对齐 records.COLUMN_ALIASES，补完联系方式即可直接 import。

    刻意不做「是不是中国公司」的强断言 —— 返回里没有可靠的广告主国别字段，
    与其瞎猜不如把判断材料（页面名、域名、语言、平台）原样带出来给人过一眼。
    """
    domain = extract_domain(ad)
    bodies = ad.get("ad_creative_bodies") or []
    return {
        "公司名称": (ad.get("page_name") or "").strip(),
        "网址": domain,
        "电话": "",   # 广告库给不到，待 wigolo 抓官网 contact 页补
        "邮箱": "",
        "行业": (bodies[0][:60].replace("\n", " ") if bodies else ""),
        "meta_page_id": ad.get("page_id", ""),
        "meta_ad_id": ad.get("id", ""),
        "meta_languages": ",".join(ad.get("languages") or []),
        "meta_platforms": ",".join(ad.get("publisher_platforms") or []),
        "signal_date_hint": (ad.get("ad_delivery_start_time") or "")[:10],
    }


def dedupe_by_page(rows: list[dict]) -> list[dict]:
    """一个广告主通常同时投几十条广告，按 page_id 收敛成一家公司一行。

    保留最早的投放日期 —— 那更接近「这家公司什么时候开始做海外投放」。
    """
    best: dict[str, dict] = {}
    for row in rows:
        key = row.get("meta_page_id") or row.get("公司名称")
        if not key:
            continue
        current = best.get(key)
        if current is None:
            best[key] = row
            continue
        # 有域名的优先；同等条件下取更早的投放日
        if not current["网址"] and row["网址"]:
            best[key] = row
        elif row["signal_date_hint"] and (
            not current["signal_date_hint"] or row["signal_date_hint"] < current["signal_date_hint"]
        ):
            best[key] = row
    return list(best.values())
