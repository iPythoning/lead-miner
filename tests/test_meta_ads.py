"""Meta 广告库转换层测试。不打真实 API —— 用固定样本锁住转换行为。

样本结构基于既有知识构造，若 probe 显示真实结构不同，改样本再改代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miner.collectors.meta_ads import (  # noqa: E402
    ad_to_row,
    dedupe_by_page,
    extract_domain,
    token_shape_problem,
)

SAMPLE = {
    "id": "1234567890",
    "page_id": "page_001",
    "page_name": "Ningbo Foo Tools",
    "ad_creative_link_captions": ["WWW.NINGBOFOO.COM"],
    "ad_creative_link_titles": ["Wholesale Hand Tools"],
    "ad_creative_bodies": ["Factory direct hand tools\nOEM welcome"],
    "ad_delivery_start_time": "2026-07-15T00:00:00+0000",
    "publisher_platforms": ["facebook", "instagram"],
    "languages": ["en"],
}


def test_extract_domain_from_caption():
    assert extract_domain(SAMPLE) == "ningbofoo.com"


def test_extract_domain_strips_www_and_lowercases():
    assert extract_domain({"ad_creative_link_captions": ["WWW.Example.COM"]}) == "example.com"


def test_extract_domain_ignores_prose_titles():
    # 标题是文案不是域名，不能误当域名（含空格 / 无点）
    ad = {"ad_creative_link_captions": [], "ad_creative_link_titles": ["Wholesale Hand Tools"]}
    assert extract_domain(ad) == ""


def test_extract_domain_handles_missing_fields():
    assert extract_domain({}) == ""
    assert extract_domain({"ad_creative_link_captions": None}) == ""
    assert extract_domain({"ad_creative_link_captions": [""]}) == ""


def test_ad_to_row_uses_import_compatible_headers():
    row = ad_to_row(SAMPLE)
    # 列名必须落在 records.COLUMN_ALIASES 里，补完联系方式就能直接 import
    assert row["公司名称"] == "Ningbo Foo Tools"
    assert row["网址"] == "ningbofoo.com"
    assert row["电话"] == ""      # 广告库给不到，留空待 wigolo 补
    assert row["邮箱"] == ""
    assert row["signal_date_hint"] == "2026-07-15"
    assert row["meta_languages"] == "en"
    assert "\n" not in row["行业"]  # 创意文案换行会破坏 CSV


def test_ad_to_row_tolerates_sparse_ad():
    row = ad_to_row({"id": "x"})
    assert row["公司名称"] == ""
    assert row["网址"] == ""
    assert row["signal_date_hint"] == ""


def test_dedupe_by_page_collapses_one_advertiser_to_one_row():
    # 一个广告主同时投几十条广告，必须收敛成一家一行
    rows = [ad_to_row(SAMPLE), ad_to_row({**SAMPLE, "id": "999"})]
    assert len(dedupe_by_page(rows)) == 1


def test_dedupe_prefers_row_with_domain():
    no_domain = ad_to_row({**SAMPLE, "ad_creative_link_captions": [], "ad_creative_link_titles": []})
    with_domain = ad_to_row(SAMPLE)
    assert dedupe_by_page([no_domain, with_domain])[0]["网址"] == "ningbofoo.com"
    assert dedupe_by_page([with_domain, no_domain])[0]["网址"] == "ningbofoo.com"


def test_dedupe_keeps_earliest_delivery_date():
    # 最早投放日更接近「这家公司什么时候开始做海外投放」
    later = ad_to_row(SAMPLE)
    earlier = ad_to_row({**SAMPLE, "id": "888", "ad_delivery_start_time": "2026-03-01T00:00:00+0000"})
    assert dedupe_by_page([later, earlier])[0]["signal_date_hint"] == "2026-03-01"


def test_dedupe_skips_rows_without_identity():
    assert dedupe_by_page([ad_to_row({"id": "x"})]) == []


def test_token_shape_catches_copy_paste_accidents():
    """Meta 对格式坏掉的 token 一律回 `Cannot parse access token`，本地先筛掉。"""
    good = "EAA" + "x" * 180
    assert token_shape_problem(good) == ""

    assert "引号" in token_shape_problem(f'"{good}"')
    assert "引号" in token_shape_problem(f"'{good}'")
    assert "空格或换行" in token_shape_problem("EAAxxx" + "y" * 100 + "\nzzz")
    assert "Bearer" in token_shape_problem("Bearer " + good)
    assert "太短" in token_shape_problem("1234567890")          # App ID 长这样
    assert "太短" in token_shape_problem("a1b2c3d4e5f6a7b8c9d0")  # App Secret 长这样


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} ({failures} failed)")
    raise SystemExit(1 if failures else 0)
