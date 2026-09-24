---
name: paibao-lead-miner
description: 外贸名录线索采集端。输入展会名录/平台会员名单 CSV，规范化、工商补全、批内去重后推送线索池网关（双写企微 SCRM + trade-crm）。触发词：名录导入、线索采集、工商补全、线索去重、lead ingest。
---

# 外贸名录线索采集（Lead Miner）

把展会名录/平台会员名单变成干净、可打分、可跟进的线索池增量。零第三方依赖（Python 标准库）。

## 角色

名录采集操作员 + 数据合规守门员。只做规范化/补全/去重/推送四件事。

## 四条边界（越界就是 bug）

1. **本仓不抓网页**：只吃 wigolo 导出的 CSV/JSON。调官方 API（如 `collectors/meta_ads.py`）不算抓网页，但产出也只是待补全 CSV。
2. **本仓不打分**：ICP 分由网关 `scoreColdLead` 算，不本地复算（必漂移）。
3. **本仓不直连 trade-crm**：只打网关 `/v1/ingest/batch` 一个入口。
4. **PIPL 红线**：不碰个人手机号、个人邮箱、简历数据；只用企业公示联系方式。

## 标准流程

```bash
ssh -N -L 18090:127.0.0.1:18090 aliyun   # 网关只在内网，先开隧道（另终端保持）
export OUTBOUND_SITE_KEY=...             # 网关 LEAD_SITE_KEYS_JSON 里 outbound site 的 key，本仓不存
python3 -m miner import 名录.csv --source canton-fair-138 --signal-date 2026-04-15 --dry-run
# 确认载荷无误后去掉 --dry-run；先用 --limit 50 试水
```

关键参数：`--source`（信号源标识，幂等键前缀） · `--signal-date`（直接决定 ICP 分新鲜度衰减） · `--assume-import-export`（广交会这类必然出口企业置位，省逐家查工商） · `--no-enrich`（跳过工商补全）。

## 工商补全与去重

- 补全只拿两样：统一社会信用代码（跨源去重稳定身份）、经营范围有无进出口权。端点全部 env 注入（`ENRICH_API_URL/KEY`），未配置自动降级 NoopEnricher。
- 无补全又无 `--assume-import-export` 时满分只有 0.40 → 整批落 low band，CLI 会告警。
- 电话保留 8–15 位数字（分机切除），与网关 `normalizePhone` 对齐；CSV 用 `utf-8-sig` 读。

## 产品互链

下游 AI 探客（paibaowork.com / Console `/leads`）的画像与评分口径不得低于本仓：来源优先级矩阵见 `docs/sources.md`，合规红线见本文件与 `AGENTS.md`。
