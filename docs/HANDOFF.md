# 交接状态 · HANDOFF（paibao-lead-miner）

> 开工前必读，收工前必更新。历史变更看 git log。


> 📐 **先读 [`docs/adr/0001-lead-engine-strategy.md`](adr/0001-lead-engine-strategy.md)**：
> 信号源为什么这么选、leads.paibao.ai 为什么这样落位、合规红线在哪。
> 不读会把本仓当成「一个导数据的脚本」，从而在错误的地方优化。

## 2026-10-14 · CRM 标签/备注中文化（miner 侧）

- 改动：`records.py` 新增 note 列（别名 `备注`/`note`）→ `visitor.message`（网关映射进 CRM notes）；新增 `signal_label` 字段 → `meta.signal_label`；`cli.py` 新增 `--signal-label`（缺省回退 `--source` slug）。tests 全绿。
- 密钥处理收紧：site key 只由网关侧授权进程注入调用，本仓不存 key、不读、不打印、不导出到本地环境。

## 2026-08-20 · 2GIS 采集器落地（FDE ABM 池,ADR-0001 D2 右半边）

**已完成**:`miner/collectors/two_gis.py` + CLI `2gis-probe`/`2gis-fetch` + `tests/test_two_gis.py`
(4 测试绿,三个测试文件全绿)。信号源 = 2GIS 中亚商户目录(阿拉木图卡车类目 440+ 家,
自带**企业公示电话**——合规红线内,且与 Meta 广告库不同**无需 wigolo 补全**,CSV 直接
`miner import --source 2gis-kz-truck`)。用途:给梁山半挂车客户(富源/山东朝阳汽车)建
哈萨克/乌兹别克买家 ABM 池,与海关镜像数据交叉验证(2GIS 定「谁」,贸易数据定「量与真伪」)。
**⛔ 卡一件**:`TWOGIS_API_KEY`(dev.2gis.com 免费注册,用户动作)。端点字段未经真 key
核实——**先 `python3 -m miner 2gis-probe` 再 fetch**(meta_ads 同哲学),probe 显示结构
不同就改样本再改代码。
**下一步**:key 到手 → probe → `2gis-fetch --city Алматы -o /tmp/kz.csv` → dry-run import
→ 真 import;kolesa.kz(挂中国车的车商=最热层)属网页抓取,归 wigolo,批2。
**验证**:`python3 tests/test_two_gis.py`。


## 项目速览

- 技术栈：Python 3.11+，零第三方依赖
- 远程：https://github.com/iPythoning/paibao-lead-miner
- 下游：`paibao-wechat-scrm` 网关 `/v1/ingest/batch` → 企微 SCRM 待激活池 + trade-crm

### 跨仓链路（本仓是起点，全貌指针放这里）

```
paibao-lead-miner（采集 CLI）→ paibao-wechat-scrm（网关 lane 3 冷名单车道）
   → 双写：企微 SCRM 待激活池（上层漏斗） + paibao-trade-crm（核心 CRM，system of record）
```

- **CRM 侧现状与下一轮任务**（广交会入库结果、i18n、lifecycleStage、挂起决策）：
  `paibao-trade-crm/docs/HANDOFF.md` 顶部「外贸获客线索线」条目
- **网关侧**：`paibao-wechat-scrm/docs/{HANDOFF,lead-ingest-edge}.md`

## 如何验证（基线，动手前先跑一次）

```bash
python3 tests/test_miner.py && python3 tests/test_meta_ads.py
```

## 当前目标

外贸获客第一批名单落地：广交会参展商名录 → 线索池 → 企微 SCRM + trade-crm 双写。

## 已完成（2026-08-14）

- 网关侧（`paibao-wechat-scrm` commit `9ba6caa`）：冷名单车道 lane 3 +
  `POST /v1/ingest/batch`，`go vet` / `go test -race` 全绿。**已部署上线并端到端验证**
  （另修了座机被存成印度号码的 bug，commit `b3f33f8`）。
- 本仓 P0：CSV 规范化（中英文列名别名、分机切除、多号码取首、全角归一）、
  批内去重（保留最完整行）、配置驱动的工商补全、批量推送与结果汇总。18 个测试全绿。
- 端到端 dry-run 通过（脏数据样本：分机、多号码、全角重复行、空公司名、"暂无"电话）。

## 下一步（按顺序）

1. ~~部署网关~~ **✅ 2026-08-14 已完成**（`release.sh ebc12f7 deploy-gateway`），
   冒烟四项全 PASS，trade-crm 侧实查 `source=OUTBOUND` 落库。
   outbound site key is held only by operations; this repository never stores it. **Never read, print, or export it to a local environment**. An authorized gateway-side process injects it and calls `127.0.0.1:18090`, returning only batch counts and request_id results.
2a. **Meta 广告库路线**（不依赖任何人给 CSV，可立即开始）：
   ```bash
   export META_ACCESS_TOKEN=...
   python3 -m miner meta-probe          # 必须先跑，确认权限与字段
   python3 -m miner meta-fetch --terms "<品类>" -o meta.csv
   ```
   产出是半成品（有公司名+域名，无电话）→ wigolo 补 contact 页的电话/邮箱两列 → import。
   详见 docs/sources.md。**probe 未通过前不要批量 fetch。**

2b. **展会名录路线**：拿到广交会参展商名录 CSV 后，先在本仓运行 `python3 -m miner import 名录.csv --source canton-fair-138 --signal-date <展会开幕日> --assume-import-export --limit 50 --dry-run`；人工核对载荷后，由 aliyun 内部进程安全读取现有 site key 再发 batch。禁止把 key 导出到本机、shell history 或日志；结果只记录 created/duplicate/failed。
3. **人工跟进 50 家**，测转化率与话术 —— 这一步的结论决定要不要做后续信号源
4. 后续：ICP high band 自动转首触任务的阈值（用户已确认要，但等第 3 步数据）

## 冒烟测试数据（2026-08-14）— ✅ 已清理

网关首触任务 `task_9b2aa62f…` 已 complete(result=invalid)；trade-crm 3 条 Lead 已软删
（`deletedAt`，可恢复）。paibaowork 站已无 2026-08-14 的 open 任务。

**14 条 2026-08-03/04 的历史遗留测试任务也已一并清理**（complete/invalid，线索保留在池内）。
两站当前未完成任务均为 0，销售队列干净。

## ⚠️ 电话格式：座机必须转 E.164（网关已处理，采集端勿绕过）

trade-crm 的 `normalizePhone` 是印度 fork 残留：`0` 开头会被加 `91`
（`057400000002` → `9157400000002`，印度号码，打不通）。网关侧 `tradecrmPhone()`
已把座机转成 `86…` 绕开（commit `b3f33f8`）。

**采集端要做的是：把座机原样（`0574…`）传给网关，不要自己加国家码** ——
网关的 `normalizePhone` 与 subject_id 去重都基于国内格式，采集端擅自转换会导致
同一家公司在池里出现两条。

## 生产拓扑（运维细节不公开，公开版只保留契约）

- **lead 网关**：内网容器监听 `127.0.0.1:18090`，带线索池双写（企微 SCRM + trade-crm）。
- **公网不暴露 ingest**：公网入口被反代挡在 4xx；这是对的设计，不要为了方便去改反代开放它
  —— 等于给公网开一个能写线索池的端点。
- 因此 miner 走 ssh 隧道连内网网关（见 README 用法）。

部署前基线（用于验证部署是否真的生效）：
`POST 127.0.0.1:18090/v1/ingest/submissions` → 401；`/v1/ingest/batch` → 404。
**部署后 batch 必须变成 401** —— 从 404 到 401 就是新代码上线的铁证。

## 已知缺口

- **爱企查端点未核实**：写作时 WebSearch 不可用，故 `enrich.py` 做成配置驱动，
  不写死任何 provider。拿到真实凭据后填 `ENRICH_API_URL` / `ENRICH_FIELD_MAP` 即可。
- **待激活池没有 UI**：冷线索 `status=pending` 存在网关 bolt 里，
  目前只能从 trade-crm dashboard `/leads`（`source=OUTBOUND`）看。网关侧无列表接口。
- **无电话线索进不了 trade-crm**（`Contact.phoneNumber` 必填），留在网关池内。
- 单批 200 条、逐条独立 bolt 事务；导入量级涨到十万条再考虑单事务批写。

## 最近交接记录

| 日期 | 操作者 | 摘要 |
|---|---|---|
| 2026-08-14 | Claude | 建仓 + P0 采集链路；网关 lane 3 已上线验证；Meta 广告库 collector |

