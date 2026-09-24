"""2GIS 转换层测试。不打真实 API —— 固定样本锁住转换行为。

样本结构基于既有知识构造,若 probe 显示真实结构不同,改样本再改代码。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miner.collectors.two_gis import dedupe, item_to_row, pick_contact  # noqa: E402
from miner.records import COLUMN_ALIASES  # noqa: E402

SAMPLE = {
    "name": "Universal Machinery Group",
    "adm_div": [{"name": "Казахстан"}, {"name": "Алматы"}],
    "rubrics": [{"name": "Продажа грузовых автомобилей"}, {"name": "Спецтехника"}],
    "contact_groups": [
        {"contacts": [
            {"type": "phone", "value": "+77271234567", "text": "+7 (727) 123-45-67"},
            {"type": "website", "value": "https://umg.kz"},
        ]},
        {"contacts": [{"type": "email", "value": "sales@umg.kz"}]},
    ],
}


def test_item_to_row_fields():
    row = item_to_row(SAMPLE, country="KZ")
    assert row["company"] == "Universal Machinery Group"
    assert row["phone"] == "+77271234567"          # value(规范格式)优先于 text
    assert row["email"] == "sales@umg.kz"
    assert row["website"] == "https://umg.kz"
    assert row["province"] == "KZ-Алматы"           # 末级行政区 = 城市
    assert "Продажа грузовых автомобилей" in row["industry"]


def test_row_columns_are_importable():
    # CSV 列名必须能被 miner import 的语义列识别,否则采完白采。
    row = item_to_row(SAMPLE, country="KZ")
    for col in ("company", "phone", "email", "website", "province", "industry"):
        assert any(col == a.lower() for a in COLUMN_ALIASES[col]), col
        assert col in row


def test_pick_contact_missing_kind_and_empty_groups():
    assert pick_contact(None, "phone") == ""
    assert pick_contact([{"contacts": []}], "phone") == ""


def test_dedupe_by_name_phone_and_drops_nameless():
    a = item_to_row(SAMPLE, country="KZ")
    dup = dict(a)                      # 同名同号(分店场景)
    nameless = dict(a, company="")     # 无名条目丢弃
    other = dict(a, phone="+77070000000")  # 同名不同号保留
    out = dedupe([a, dup, nameless, other])
    assert out == [a, other]


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
