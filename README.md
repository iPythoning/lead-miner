# paibao-lead-miner

外贸名录 → 线索池采集端。把展会名录/平台会员名单规范化、补全、去重后推给网关，
由网关一次双写**企微 SCRM**（销售跟进面）与 **trade-crm**（CRM of record）。

零第三方依赖，只用 Python 标准库。

## 为什么

选源标准、产品落位与合规红线见 [`docs/adr/0001-lead-engine-strategy.md`](docs/adr/0001-lead-engine-strategy.md)。

## 信号源

见 [`docs/sources.md`](docs/sources.md) —— 优先级矩阵、逐源获取方式、合规红线。
短版：**Meta 广告库 API 最该先做**（免费官方 API、投英文广告=已在为海外获客花钱、不用等任何人给 CSV）。

## 边界

- **本仓不抓网页。** 抓取由 wigolo 完成，这里只吃它导出的 CSV/JSON。
- **本仓不打分。** ICP 分由网关 `scoreColdLead` 算，随响应返回。重复实现必然漂移。
- **本仓不直连 trade-crm。** 只打网关一个入口，双写与去重是网关的事。

```
wigolo 抓取 → CSV → [ 本仓：规范化 / 工商补全 / 批内去重 ] → POST /v1/ingest/batch
                                                                      ↓
                                          企微 SCRM 待激活池  +  trade-crm（source=OUTBOUND）
```

## 用法

网关的 ingest 只在内网监听，**不走公网**（site_key 能写线索池，不该有公网入口）。
先开隧道：

```bash
ssh -N -L 18090:127.0.0.1:18090 aliyun     # 另开一个终端保持
```

```bash
export OUTBOUND_SITE_KEY=...            # 网关 LEAD_SITE_KEYS_JSON 里 outbound site 的 key
python3 -m miner import 名录.csv --source canton-fair-135 --signal-date 2026-04-15 --dry-run
```

`GATEWAY_URL` 默认 `http://127.0.0.1:18090`（隧道端点）。也可以直接把本仓拷到 aliyun 上跑。

确认载荷无误后去掉 `--dry-run`。先用 `--limit 50` 试水。

| 参数 | 说明 |
|---|---|
| `--source` | 信号源标识，进 `meta.signal_source`，也是幂等键前缀 |
| `--signal-date` | 展会开幕日/名录发布日，`YYYY-MM-DD`。**直接决定 ICP 分的新鲜度衰减** |
| `--assume-import-export` | 信号源本身即蕴含进出口资质时置位（广交会参展商必然是出口企业），省掉逐家查工商 |
| `--limit N` | 只处理前 N 条 |
| `--dry-run` | 只打印实际载荷，不推送 |
| `--no-enrich` | 跳过工商补全 |

## ICP 打分（网关侧，此处仅供参考）

进出口资质 0.35 + 规模 20–300 人 0.25 + 电话 0.20 + 官网 0.10 + 邮箱 0.10，
再乘信号新鲜度衰减（≤90 天 ×1.0，≤1 年 ×0.85，更旧或缺日期 ×0.70）。

**没有工商补全、也没加 `--assume-import-export` 时，满打满算只有 0.40 → 整批落 low band。**
CLI 会就此告警。展会名录直接用 `--assume-import-export`；平台会员等来源建议配补全。

## 工商补全

补全只为两件事：拿统一社会信用代码（跨源去重的稳定身份）、判经营范围有无进出口权。

端点与字段映射**从环境变量注入，代码里不写死任何 provider**：

```bash
ENRICH_API_URL='https://<你的端点>?keyword={name}'   # {name} 占位公司名
ENRICH_API_KEY=...
ENRICH_AUTH_HEADER=Authorization                     # 默认
ENRICH_AUTH_PREFIX='Bearer '                         # 需要时填
ENRICH_FIELD_MAP='{"credit_code":"data.creditCode","scope":"data.scope","staff_size":"data.employeeCount"}'
```

未配置时自动降级为 NoopEnricher，链路照常跑通。补全单家失败不丢线索，只是分数低一档。

## 联系方式与合规

- 只用**企业公示**联系方式（工商公示座机、官网 info@/sales@、名录登记电话）。
- **不采集个人手机号、个人邮箱、简历数据** —— PIPL 下无合法性基础。
- 企业座机（`0574-12345678` → 12 位）是主力联系方式，网关保留 8–15 位数字，
  分机段由本仓切除（不切会超 15 位被网关整条丢弃）。

## 验证

```bash
python3 tests/test_miner.py
```
