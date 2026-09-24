# AGENTS.md — paibao-lead-miner

外贸名录采集端。Python 3.11+，**零第三方依赖**（只用标准库）。

全局规则以 `~/AGENTS.md` 为准；本文件只写本仓特有的。

## 验证命令（动手前先跑一次，确认基线绿）

```bash
python3 tests/test_miner.py
```

## 架构边界（越界就是 bug）

| 职责 | 归属 | 本仓禁止 |
|---|---|---|
| 抓网页 | wigolo | 不写爬虫，只吃导出的 CSV/JSON。**调官方 API 不算抓网页**（如 `collectors/meta_ads.py`），但它产出的也只是待补全 CSV，补联系方式仍归 wigolo |
| 打分 | 网关 `scoreColdLead` | 不本地复算 ICP 分（必漂移），分数从 batch 响应读 |
| 去重与双写 | 网关线索池 | 不直连 trade-crm，只打 `/v1/ingest/batch` |
| 工商补全 | 本仓 `enrich.py` | 不写死任何 provider 端点，全部 env 注入 |

## 红线

- **不采集个人信息**：个人手机号、个人邮箱、简历数据一律不碰（PIPL）。只用企业公示联系方式。
- **不写死第三方 API 端点**：写作时无法核实爱企查真实端点，故设计为配置驱动。
  拿到真实凭据填 env 即可，不改代码。
- **电话规范化必须与网关 `normalizePhone` 保持一致**（保留 8–15 位数字）。改动前先看
  `paibao-wechat-scrm/lead_subject.go`，两边不一致会导致线索静默进不了 trade-crm。
- 禁止 `git add -A`；commit 前 `git diff --stat`。

## 已知坑

- **分机号**：名录里 `0574-12345678-8001` 很常见，不切分机会超 15 位被网关整条丢弃。
- **Excel 导出 BOM**：CSV 一律用 `utf-8-sig` 读。
- **全角字符**：公司名里的全角括号/空格会让同一家公司产生两条，`normalize_company` 负责归一。
- **无补全 = 全 low band**：进出口资质(0.35)+规模(0.25) 都缺时上限 0.40。展会名录用
  `--assume-import-export`。

---

# 跨 agent 交接协议（通用，勿删）

进入本仓前：读 `README.md` → 本文件 → `docs/HANDOFF.md` → `git log -10 --oneline`，
跑一次上面的验证命令确认基线绿。**基线红先修基线，不在红基线上叠新改动。**

收工前：跑验证 → 更新 `docs/HANDOFF.md` → 提交，不留半成品。
