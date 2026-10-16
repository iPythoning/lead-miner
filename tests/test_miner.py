"""跑：python3 -m pytest tests/ -q    或    python3 tests/test_miner.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miner.enrich import apply_enrichment, dig, has_import_export, parse_staff_size  # noqa: E402
from miner.push import chunk, summarize  # noqa: E402
from miner.records import (  # noqa: E402
    CompanyRecord,
    dedupe,
    from_row,
    normalize_company,
    normalize_phone,
    normalize_website,
)


def test_normalize_phone_keeps_landline():
    # 座机是这条车道的主力联系方式，必须活下来（网关保留 8-15 位数字）。
    assert normalize_phone("0574-12345678") == "057412345678"
    assert normalize_phone("021-6789 1234") == "02167891234"
    assert normalize_phone("13800138000") == "13800138000"
    assert normalize_phone("+86 13800138000") == "13800138000"


def test_normalize_phone_strips_extension():
    # 不切分机会超过 15 位，被网关整条丢弃 —— 名录里极常见的坑。
    assert normalize_phone("0574-12345678-8001") == "057412345678"
    assert normalize_phone("0574-12345678转8001") == "057412345678"
    assert normalize_phone("0574-12345678 ext.8001") == "057412345678"


def test_normalize_phone_takes_first_of_many():
    assert normalize_phone("0574-12345678/0574-87654321") == "057412345678"
    assert normalize_phone("13800138000、13900139000") == "13800138000"


def test_normalize_phone_rejects_garbage():
    assert normalize_phone("") == ""
    assert normalize_phone("暂无") == ""
    assert normalize_phone("123") == ""  # 太短


def test_normalize_company_ignores_width_and_space():
    assert normalize_company("宁波　某某（集团）有限公司") == normalize_company("宁波某某(集团)有限公司")


def test_from_row_maps_chinese_and_english_headers():
    zh = from_row(
        {"公司名称": "宁波某某进出口有限公司", "联系电话": "0574-12345678", "邮箱": "Sales@Example.com", "展位号": "12.1A23"},
        source="canton-fair-135",
        signal_date="2026-04-15",
    )
    assert zh is not None
    assert zh.phone == "057412345678"
    assert zh.email == "sales@example.com"
    assert zh.booth == "12.1A23"

    en = from_row(
        {"company": "Ningbo Foo Import & Export Co.", "tel": "0574-87654321", "website": "www.example.com"},
        source="canton-fair-135",
        signal_date="2026-04-15",
    )
    assert en is not None
    assert en.website == "http://www.example.com"


def test_from_row_skips_rows_without_company():
    assert from_row({"公司名称": "", "联系电话": "0574-1234"}, "s", "2026-01-01") is None


def test_request_id_is_stable_and_source_scoped():
    a = CompanyRecord(company="宁波某某有限公司", source="canton-fair-135", signal_date="2026-04-15")
    b = CompanyRecord(company="宁波　某某有限公司", source="canton-fair-135", signal_date="2026-04-15")
    assert a.request_id == b.request_id  # 全角/空格差异不该产生两条

    other_source = CompanyRecord(company="宁波某某有限公司", source="alibaba-new", signal_date="2026-04-15")
    assert a.request_id != other_source.request_id  # 不同信号源各记一条

    with_code = CompanyRecord(company="宁波某某有限公司", source="canton-fair-135",
                              signal_date="2026-04-15", credit_code="91330200MA2XXXX")
    assert with_code.request_id.endswith("91330200MA2XXXX")


def test_dedupe_keeps_most_complete_row():
    sparse = CompanyRecord(company="某某公司", source="s", signal_date="2026-04-15")
    rich = CompanyRecord(company="某某公司", source="s", signal_date="2026-04-15",
                         phone="057412345678", email="a@b.com", website="http://x.com")
    assert dedupe([sparse, rich])[0].phone == "057412345678"
    assert dedupe([rich, sparse])[0].phone == "057412345678"  # 顺序无关
    assert len(dedupe([sparse, rich])) == 1


def test_to_ingest_item_shape():
    record = CompanyRecord(
        company="宁波某某进出口有限公司", source="canton-fair-135", signal_date="2026-04-15",
        phone="057412345678", email="sales@example.com", website="http://example.com",
        credit_code="91330200MA2XXXX", staff_size=80, import_export=True, province="浙江",
        note="来自 SSL 证书扫描：证书 29 天后到期", signal_label="SSL证书扫描",
    )
    item = record.to_ingest_item()
    assert item["request_id"] == "canton-fair-135-91330200MA2XXXX"
    assert item["visitor"]["phone"] == "057412345678"
    assert item["visitor"]["message"] == "来自 SSL 证书扫描：证书 29 天后到期"
    assert item["occurred_at"] == "2026-04-15T00:00:00Z"
    meta = item["meta"]
    assert meta["import_export"] is True
    assert meta["has_website"] is True
    assert meta["site_url"] == "http://example.com"
    assert meta["staff_size"] == 80
    assert meta["signal_source"] == "canton-fair-135"
    assert meta["signal_label"] == "SSL证书扫描"


def test_has_import_export():
    assert has_import_export("货物进出口;技术进出口;五金销售")
    assert has_import_export("从事对外贸易经营活动")
    assert not has_import_export("五金制品制造;金属材料销售")


def test_parse_staff_size_handles_ranges():
    assert parse_staff_size(80) == 80
    assert parse_staff_size("50人") == 50
    assert parse_staff_size("50-99人") == 50  # 区间取下界，保守
    assert parse_staff_size(None) == 0
    assert parse_staff_size("少于50人") == 50


def test_dig_walks_paths_safely():
    payload = {"data": {"company": {"creditCode": "9133"}}}
    assert dig(payload, "data.company.creditCode") == "9133"
    assert dig(payload, "data.missing.x") is None
    assert dig(payload, "nope") is None


def test_apply_enrichment_fills_gaps_without_overwriting():
    record = CompanyRecord(company="某某", source="s", signal_date="2026-04-15", phone="057400000000")
    payload = {"data": {"creditCode": "91330200MA2XXXX", "scope": "货物进出口;五金销售",
                        "employeeCount": "50-99人", "telephone": "0574-99999999"}}
    field_map = {"credit_code": "data.creditCode", "scope": "data.scope",
                 "staff_size": "data.employeeCount", "phone": "data.telephone"}
    out = apply_enrichment(record, payload, field_map)

    assert out.credit_code == "91330200MA2XXXX"
    assert out.import_export is True
    assert out.staff_size == 50
    assert out.phone == "057400000000"  # 名录自带的电话更新，不被覆盖


def test_apply_enrichment_tolerates_missing_fields():
    record = CompanyRecord(company="某某", source="s", signal_date="2026-04-15")
    out = apply_enrichment(record, {"data": {}}, {"credit_code": "data.creditCode"})
    assert out.credit_code == ""
    assert out.import_export is False


def test_chunk_respects_batch_size():
    assert [len(c) for c in chunk(list(range(450)), 200)] == [200, 200, 50]
    assert list(chunk([], 200)) == []


def test_summarize_aggregates_bands_and_failures():
    responses = [
        {"total": 2, "created": 1, "duplicate": 1, "failed": 0,
         "items": [{"status": "created", "score_band": "high"}, {"status": "duplicate", "score_band": "mid"}]},
        {"total": 2, "created": 1, "duplicate": 0, "failed": 1,
         "items": [{"status": "created", "score_band": "high"},
                   {"status": "failed", "request_id": "x", "error": "phone or wechat is required"}]},
    ]
    report = summarize(responses)
    assert report["totals"] == {"total": 4, "created": 2, "duplicate": 1, "failed": 1}
    assert report["bands"] == {"high": 2, "mid": 1}
    assert report["failures"][0]["request_id"] == "x"


def test_normalize_website():
    assert normalize_website("www.example.com") == "http://www.example.com"
    assert normalize_website("https://example.com") == "https://example.com"
    assert normalize_website("无") == ""
    assert normalize_website("") == ""


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

