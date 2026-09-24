"""命令行入口。

    python3 -m miner import 名录.csv --source canton-fair-135 --signal-date 2026-04-15
    python3 -m miner import 名录.csv --source canton-fair-135 --signal-date 2026-04-15 --dry-run
    python3 -m miner import 名录.csv --source canton-fair-135 --signal-date 2026-04-15 --limit 50

先 --dry-run 看规范化结果和缺联系方式的比例，确认没问题再真推。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from .collectors import meta_ads, two_gis
from .enrich import build_enricher
from .push import PushError, chunk, gateway_url, push_batch, site_key, summarize
from .records import dedupe, load_table


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miner", description="外贸名录 → 线索池（企微 SCRM + trade-crm 双写）")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="导入名录 CSV")
    imp.add_argument("csv", type=Path, metavar="FILE", help="名录文件（.csv 或 .xlsx）")
    imp.add_argument("--source", required=True, help="信号源标识，如 canton-fair-135")
    imp.add_argument("--signal-date", required=True, help="信号日期 YYYY-MM-DD（展会开幕日/名录发布日）")
    imp.add_argument("--limit", type=int, default=0, help="只处理前 N 条（试水用）")
    imp.add_argument("--category", default="",
                     help="按品类筛选，逗号分隔（匹配展位号里的品类段，如 五金,工具）。"
                          "名录按品类排序，不筛就 --limit 会全取到同一个品类")
    imp.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    imp.add_argument("--resync", action="store_true",
                     help="重放补推：已存在的线索也重推一次 trade-crm（对方限流丢批后用）")
    imp.add_argument("--no-enrich", action="store_true", help="跳过工商补全")
    imp.add_argument(
        "--no-email-check",
        action="store_true",
        help="跳过邮箱清洗（默认开启：MX 检查；--no-smtp 可只做 MX 层）",
    )
    imp.add_argument(
        "--no-smtp",
        action="store_true",
        help="邮箱清洗只做语法+MX，不做 SMTP RCPT 握手（批量大/网络受限时用，快很多）",
    )
    imp.add_argument(
        "--assume-import-export",
        action="store_true",
        help="信号源本身即蕴含进出口资质时置位（如广交会参展商必然是出口企业），省掉逐家查工商",
    )

    sub.add_parser("meta-token-check", help="自检手上的 Meta token 能不能查广告库（先跑这个）")

    probe = sub.add_parser("meta-probe", help="探测 Meta 广告库 API 的真实结构与权限（批量拉取前必跑）")
    probe.add_argument("--countries", default=",".join(meta_ads.DEFAULT_COUNTRIES),
                       help="广告触达国家，逗号分隔（默认欧盟主要市场，DSA 下非政治广告可查）")

    fetch = sub.add_parser("meta-fetch", help="拉取 Meta 广告库 → CSV（待补联系方式后再 import）")
    fetch.add_argument("--countries", default=",".join(meta_ads.DEFAULT_COUNTRIES))
    fetch.add_argument("--terms", default="", help="搜索词，如产品品类")
    fetch.add_argument("--max-pages", type=int, default=5, help="分页上限，防第一次跑就撞配额")
    fetch.add_argument("-o", "--out", type=Path, required=True, help="输出 CSV 路径")

    gprobe = sub.add_parser("2gis-probe", help="探测 2GIS Catalog API 真实结构与 key 权限（批量拉取前必跑）")
    gprobe.add_argument("--city", default="Алматы", help="城市名（俄语，默认阿拉木图）")

    gfetch = sub.add_parser("2gis-fetch", help="拉取 2GIS 商用车类目商户 → CSV（自带公示电话，可直接 import）")
    gfetch.add_argument("--city", default="Алматы", help="城市名（俄语）")
    gfetch.add_argument("--country", default="KZ", help="国家码，写进 province 列前缀")
    gfetch.add_argument("--queries", default=",".join(two_gis.DEFAULT_QUERIES),
                        help="检索词，逗号分隔（默认商用车四词：卡车销售/半挂车/工程机械/货运）")
    gfetch.add_argument("--max-pages", type=int, default=10, help="每词分页上限")
    gfetch.add_argument("-o", "--out", type=Path, required=True, help="输出 CSV 路径")
    return parser


def cmd_meta_token_check(args: argparse.Namespace) -> int:
    """回答「我这个 token 是不是广告的」—— 靠实际调用，不靠翻文档。"""
    d = meta_ads.diagnose_token()

    if who := d.get("identity"):
        print(f"✅ token 有效，属于：{who.get('name')} (id={who.get('id')})")
    else:
        print(f"❌ token 无效或已过期：\n   {d.get('identity_error')}")
        print("\n   token 有有效期。Graph API Explorer 生成的短期 token 通常几小时就过期。")
        return 1

    scopes = d.get("granted_scopes")
    if scopes is not None:
        print(f"\n已授权权限（{len(scopes)} 项）：")
        print("   " + (", ".join(scopes) if scopes else "（无）"))
        for s in ("ads_read", "ads_management", "business_management"):
            print(f"   {'✓' if s in scopes else '·'} {s}")
    else:
        print(f"\n⚠️ 读取权限列表失败：{d.get('permissions_error')}")

    print()
    if d.get("ad_library") == "OK":
        print("✅ **广告库可读** —— 这个 token 就能用，直接跑 meta-probe。")
        return 0

    print("❌ 广告库读不了。Meta 原话：")
    print("   " + (d.get("ad_library_error") or "").replace("\n", "\n   "))
    print()
    print("注意：Ad Library 的门槛主要是**账号完成身份验证**，不是某个 ads_* 权限——")
    print("所以上面 scope 齐全也可能读不了。按 Meta 那段原话对症处理；若提到")
    print("identity/ID confirmation，就是要去做身份与居住地验证。")
    return 1


def cmd_meta_probe(args: argparse.Namespace) -> int:
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    print(f"探测 {meta_ads.ENDPOINT}")
    print(f"ad_reached_countries={countries} ad_type=ALL limit=1\n")
    try:
        payload = meta_ads.probe(countries)
    except meta_ads.MetaError as exc:
        print(f"❌ 探测失败：\n{exc}\n")
        print("Meta 的错误体通常直接指出原因：字段名不对 / 权限不足 / 该地区不可查。")
        print("按它说的调整后重跑，不要跳过 probe 直接 meta-fetch。")
        return 1

    data = payload.get("data", [])
    print(f"✅ 调用成功，返回 {len(data)} 条")
    if not data:
        print("⚠️  返回 0 条。可能该地区/条件下无可见广告，换 --countries 或加 --terms 再试。")
        return 0
    print("\n=== 首条原始结构（据此确认字段是否如预期）===")
    print(json.dumps(data[0], ensure_ascii=False, indent=2)[:1500])
    print("\n=== 转换成 CSV 行的样子 ===")
    print(json.dumps(meta_ads.ad_to_row(data[0]), ensure_ascii=False, indent=2))
    return 0


def cmd_meta_fetch(args: argparse.Namespace) -> int:
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    print(f"拉取中：countries={countries} terms={args.terms!r} max_pages={args.max_pages}")
    try:
        ads = meta_ads.fetch_ads(countries, args.terms, args.max_pages)
    except meta_ads.MetaError as exc:
        print(f"拉取失败：{exc}", file=sys.stderr)
        print("先跑 `python3 -m miner meta-probe` 确认权限与字段。", file=sys.stderr)
        return 1

    rows = meta_ads.dedupe_by_page([meta_ads.ad_to_row(a) for a in ads])
    print(f"广告 {len(ads)} 条 → 去重后 {len(rows)} 家广告主")
    if not rows:
        print("无数据，未写文件。")
        return 0

    with args.out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    with_domain = sum(1 for r in rows if r["网址"])
    print(f"已写 {args.out}（{with_domain}/{len(rows)} 家有落地页域名）")
    print()
    print("⚠️  这是半成品：广告库给不到电话，而 trade-crm 的 phoneNumber 必填。")
    print("    下一步用 wigolo 抓这些域名的 contact 页补『电话/邮箱』两列，再：")
    print(f"    python3 -m miner import {args.out} --source meta-ads-<年月> --signal-date <YYYY-MM-DD>")
    return 0


def cmd_2gis_probe(args: argparse.Namespace) -> int:
    try:
        payload = two_gis.probe(two_gis.require_key(), args.city)
    except two_gis.TwoGisError as exc:
        print(f"探测失败：{exc}", file=sys.stderr)
        return 1
    region = payload["region"]
    print(f"✅ region 命中：{region.get('name')} (id={region.get('id')})")
    items = ((payload["sample"].get("result") or {}).get("items")) or []
    print(f"样本 {len(items)} 条；首条转换结果：")
    if items:
        print(json.dumps(two_gis.item_to_row(items[0], country="KZ"), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload["sample"].get("meta"), ensure_ascii=False, indent=2))
    return 0


def cmd_2gis_fetch(args: argparse.Namespace) -> int:
    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    try:
        key = two_gis.require_key()
        region = two_gis.find_region(key, args.city)
        rows = two_gis.fetch(key, str(region.get("id", "")), queries,
                             country=args.country, max_pages=args.max_pages)
    except two_gis.TwoGisError as exc:
        print(f"拉取失败：{exc}", file=sys.stderr)
        print("先跑 `python3 -m miner 2gis-probe` 确认 key 与字段。", file=sys.stderr)
        return 1
    print(f"{region.get('name')}：{len(rows)} 家（去重后）")
    if not rows:
        print("无数据，未写文件。")
        return 0
    n = two_gis.write_csv(rows, str(args.out))
    with_phone = sum(1 for r in rows if r["phone"])
    print(f"已写 {args.out}（{with_phone}/{n} 家有公示电话）")
    print()
    print("下一步（自带电话，直接进正门）：")
    print(f"    python3 -m miner import {args.out} --source 2gis-{args.country.lower()}-truck --signal-date <YYYY-MM-DD> --dry-run")
    return 0


def validate_signal_date(raw: str) -> str:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--signal-date 需要 YYYY-MM-DD 格式，收到 {raw!r}")
    if parsed > date.today():
        raise SystemExit(f"--signal-date {raw} 在未来，网关会拒收")
    return raw


def cmd_import(args: argparse.Namespace) -> int:
    if not args.csv.exists():
        raise SystemExit(f"找不到文件: {args.csv}")
    signal_date = validate_signal_date(args.signal_date)

    records = load_table(args.csv, args.source, signal_date)
    print(f"读入 {len(records)} 行")
    records = dedupe(records)
    print(f"批内去重后 {len(records)} 家")
    if args.category:
        wanted = [c.strip() for c in args.category.split(",") if c.strip()]
        records = [r for r in records
                   if any(w in r.booth.split("|")[0] for w in wanted)]
        print(f"品类筛选 {wanted} → {len(records)} 家")
    if args.limit:
        records = records[: args.limit]
        print(f"--limit 截取前 {len(records)} 家")

    if args.assume_import_export:
        for record in records:
            record.import_export = True
        print("已按信号源语义置进出口资质标记（--assume-import-export）")

    enriched = False
    if not args.no_enrich:
        enricher = build_enricher()
        name = type(enricher).__name__
        enriched = name != "NoopEnricher"
        print(f"工商补全: {name}" + ("（未配置 ENRICH_API_URL/KEY，跳过补全）" if not enriched else ""))
        records = [enricher.enrich(r) for r in records]

    # 没有电话的进不了 trade-crm（Contact.phoneNumber 必填），提前报数而不是等失败。
    no_phone = [r for r in records if not r.phone]
    if no_phone:
        print(f"⚠️  {len(no_phone)} 家无可用电话，将留在网关线索池但不进 trade-crm")
    with_ie = sum(1 for r in records if r.import_export)
    print(f"有进出口资质标记: {with_ie}/{len(records)}")
    if not with_ie and not enriched:
        # 进出口资质占 ICP 分 0.35、规模占 0.25，两者都缺时满打满算只有 0.40 → 全部 low band。
        # 不预警的话，导完看到清一色 low 会以为是名单质量差，其实是没补全。
        print("⚠️  无工商补全且未置资质标记：本批 ICP 分最高约 0.40，会全部落 low band。")
        print("    广交会/行业展名录可直接加 --assume-import-export；其余源建议配 ENRICH_API_URL/KEY。")

    # 邮箱清洗（默认开启）：结果写 record.extra["email_status"]，不阻塞导入。
    if not args.no_email_check:
        from .email_clean import verify_records

        stats = verify_records(records, smtp=not args.no_smtp)
        print(
            f"邮箱清洗: 共 {stats['total']} 条 | "
            f"verified {stats['verified']} / valid_mx {stats['valid_mx']} / "
            f"invalid {stats['invalid']} / unknown {stats['unknown']} / 无邮箱 {stats['none']}"
        )

    if args.dry_run:
        for record in records[:5]:
            print(json.dumps(record.to_ingest_item(), ensure_ascii=False, indent=2))
        print(f"\n--dry-run：未推送。以上是前 {min(5, len(records))} 条的实际载荷。")
        return 0

    url, key = gateway_url(), site_key()
    print(f"推送 → {url}" + ("（resync：duplicate 也补推 trade-crm）" if args.resync else ""))
    responses = []
    for index, batch in enumerate(chunk(records), start=1):
        try:
            resp = push_batch(url, key, batch, resync=args.resync)
        except PushError as exc:
            print(f"第 {index} 批失败: {exc}", file=sys.stderr)
            return 1
        responses.append(resp)
        print(f"  批 {index}: 新增 {resp.get('created')} / 重复 {resp.get('duplicate')} / 失败 {resp.get('failed')}")

    report = summarize(responses)
    print("\n汇总:", json.dumps(report["totals"], ensure_ascii=False))
    print("ICP 分档:", json.dumps(report["bands"], ensure_ascii=False))
    for failure in report["failures"][:10]:
        print(f"  失败 {failure.get('request_id')}: {failure.get('error')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "import": cmd_import,
        "meta-token-check": cmd_meta_token_check,
        "meta-probe": cmd_meta_probe,
        "meta-fetch": cmd_meta_fetch,
        "2gis-probe": cmd_2gis_probe,
        "2gis-fetch": cmd_2gis_fetch,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
