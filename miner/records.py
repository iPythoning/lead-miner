"""名录原始行 → 规范化的 CompanyRecord。

边界：本模块**不抓网页**。抓取由 wigolo 完成，这里只吃它导出的 CSV/JSON。
理由是抓取工具已经有了，重复造一套只会多一处要维护的反爬逻辑。
"""

from __future__ import annotations

import csv
import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# 名录来源五花八门（广交会、行业展、协会名册），列名不统一。
# 这里按语义归组，取第一个命中的列。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("公司名称", "企业名称", "展商名称", "参展商", "参展商名称", "单位名称",
                "company", "company_name", "exhibitor"),
    # 手机排在座机前：外贸场景手机/微信才是主要联络方式，座机常年无人接。
    "phone": ("手机", "手机号", "移动电话", "电话", "联系电话", "公司电话", "座机",
              "mobile", "tel", "telephone", "phone"),
    "email": ("邮箱", "电子邮箱", "联系邮箱", "email", "e-mail", "mail"),
    "website": ("网址", "官网", "公司网址", "website", "url", "web"),
    "contact": ("联系人", "负责人", "contact", "contact_person"),
    "province": ("省份", "地区", "所在地", "province", "region"),
    "staff_size": ("企业规模", "人员规模", "员工人数", "规模", "staff_size", "employees"),
    "industry": ("行业", "产品", "主营产品", "经营范围", "industry", "products", "category"),
    "booth": ("摊位号", "展位号", "展位", "booth", "stand"),
    "credit_code": ("统一社会信用代码", "信用代码", "credit_code", "uscc"),
    # 备注列：进 trade-crm 的 notes（销售在 CRM 里第一眼看到的问题描述/跟进提示）。
    "note": ("备注", "note"),
}

_NON_DIGIT = re.compile(r"\D")
# 分机写法：0574-12345678-8001 / 转8001 / ext.8001。名录里极常见，
# 不切掉的话拼出来会超过 15 位，被网关 normalizePhone 整个丢弃。
_EXTENSION = re.compile(r"(?:-|转|ext\.?|分机)\s*\d{1,6}\s*$", re.IGNORECASE)


@dataclass(slots=True)
class CompanyRecord:
    company: str
    source: str
    signal_date: str
    phone: str = ""
    email: str = ""
    website: str = ""
    contact: str = ""
    province: str = ""
    industry: str = ""
    booth: str = ""
    credit_code: str = ""
    staff_size: int = 0
    import_export: bool = False
    # category 进 tags 供筛选（销售按品类分工）；tags 是 trade-crm 唯一可筛的维度。
    category: str = ""
    # 备注（如「来自 SSL 证书扫描：证书 29 天后到期」）→ 网关映射进 trade-crm 的 notes。
    note: str = ""
    # 信号源中文标签（import --signal-label），进 trade-crm tags 替代英文 slug。
    signal_label: str = ""
    tags: list[str] = field(default_factory=list)
    # extra 收纳所有未建模的列。名录字段多且各源不同，逐个建模会没完没了，
    # 但**丢字段是不可逆的**（重导 2.6 万条代价极高），所以一律兜进 customFields。
    extra: dict[str, str] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def request_id(self) -> str:
        """幂等键。同一家公司被多个信号源命中时，各源各一条；同源重复导入则去重。

        统一社会信用代码是最稳的身份；没有时退回公司名哈希（名录常有全角/空格
        差异，故先规范化再哈希）。
        """
        identity = self.credit_code or hashlib.sha1(
            normalize_company(self.company).encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.source}-{identity}"

    def to_ingest_item(self) -> dict:
        """转成网关 /v1/ingest/batch 的单条载荷。"""
        meta = {
            "signal_source": self.source,
            "signal_date": self.signal_date,
            "import_export": self.import_export,
            "has_website": bool(self.website),
        }
        if self.website:
            meta["site_url"] = self.website
        if self.staff_size:
            meta["staff_size"] = self.staff_size
        for key in ("credit_code", "province", "industry"):
            if value := getattr(self, key):
                meta[key] = value
        if self.booth:
            meta["booth"] = self.booth
        if self.category:
            meta["category"] = self.category
        if self.tags:
            meta["tags"] = self.tags
        if self.signal_label:
            meta["signal_label"] = self.signal_label
        if self.extra:
            meta["extra"] = self.extra

        visitor = {
            "company": self.company,
            "name": self.contact,
            "phone": self.phone,
            "email": self.email,
        }
        if self.note:
            # 网关把 visitor.message 映射成 trade-crm 的 notes —— 销售在 CRM 里第一眼看到。
            visitor["message"] = self.note

        return {
            "request_id": self.request_id,
            "channel": "outbound_import",
            "occurred_at": f"{self.signal_date}T00:00:00Z" if len(self.signal_date) == 10 else self.signal_date,
            "visitor": visitor,
            "meta": meta,
        }


def normalize_company(name: str) -> str:
    """公司名规范化：全角转半角、去空白与常见括号差异，仅用于身份哈希。"""
    name = name.strip()
    trans = str.maketrans("（）　【】", "() []")
    name = name.translate(trans)
    return re.sub(r"\s+", "", name)


def normalize_phone(raw: str) -> str:
    """规范化电话，与网关 normalizePhone 的取舍保持一致（保留 8-15 位数字）。

    企业公示座机（0574-12345678 → 12 位）是这条车道的主力联系方式，必须留住；
    分机段先切掉，否则总长超 15 位会被网关整条丢弃。
    """
    raw = raw.strip()
    if not raw:
        return ""
    # 多个号码用 / 、 , 分隔时取第一个
    raw = re.split(r"[/、,，;；]", raw)[0].strip()
    raw = _EXTENSION.sub("", raw)
    digits = _NON_DIGIT.sub("", raw)
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if 8 <= len(digits) <= 15:
        return digits
    return ""


def normalize_website(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.lower() in {"无", "none", "n/a", "-"}:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _pick_key(row: dict[str, str], semantic: str) -> str:
    """返回该语义实际命中的原始列名（未命中返回空）。

    consumed 记账必须用它而不是「所有能匹配的列」：广交会同时有「手机」和「电话」
    两列，二者都能匹配 phone 语义，但实际只消费了「手机」。按语义粗粒度记账会把
    「电话」（座机）也标记为已消费，导致它既不进结构化字段、也不进 extra —— 直接丢失。
    """
    normalized = {re.sub(r"\s+", "", k or "").lower(): k for k in row}
    values = {re.sub(r"\s+", "", k or "").lower(): (v or "").strip() for k, v in row.items()}
    aliases = COLUMN_ALIASES[semantic]
    for alias in aliases:
        low = alias.lower()
        if values.get(low):
            return normalized[low]
    for alias in aliases:
        for key, value in values.items():
            if value and alias.lower() in key:
                return normalized[key]
    return ""


def _pick(row: dict[str, str], semantic: str) -> str:
    """按别名表取值。列名做小写与空白归一，兼容名录导出时的空格污染。

    先精确匹配，再退化到「列名包含别名」—— 广交会名录的表头带届数前缀
    （`139摊位号`），精确匹配抓不到。包含匹配放在后面，避免它抢走精确命中。
    """
    normalized = {re.sub(r"\s+", "", k or "").lower(): (v or "").strip() for k, v in row.items()}
    aliases = COLUMN_ALIASES[semantic]
    for alias in aliases:
        if value := normalized.get(alias.lower()):
            return value
    for alias in aliases:
        for key, value in normalized.items():
            if value and alias.lower() in key:
                return value
    return ""


def load_xlsx(path: Path, source: str, signal_date: str) -> list[CompanyRecord]:
    """读 .xlsx 的第一个工作表。

    xlsx 本质是 zip + XML，标准库足够 —— 为读个表格引入 openpyxl 不值得，
    本仓的零依赖是刻意的（见 AGENTS.md）。
    只解析需要的部分：共享字符串表 + 单元格值，不管样式、公式、合并单元格。
    注意 .xls（老二进制格式）**不支持**，请另存为 .xlsx 或 .csv。
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{ns}t")) for si in root.iter(f"{ns}si")]

        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise SystemExit(f"{path.name} 里找不到工作表")
        root = ET.fromstring(z.read(sheets[0]))

    def cell_value(cell) -> str:
        if cell.get("t") == "inlineStr":
            return "".join(t.text or "" for t in cell.iter(f"{ns}t")).strip()
        node = cell.find(f"{ns}v")
        if node is None or node.text is None:
            return ""
        if cell.get("t") == "s" and node.text.isdigit():
            index = int(node.text)
            return shared[index] if index < len(shared) else ""
        return node.text.strip()

    def column_letter(ref: str) -> str:
        return re.match(r"[A-Z]+", ref or "").group(0) if re.match(r"[A-Z]+", ref or "") else ""

    rows = list(root.iter(f"{ns}row"))
    if not rows:
        return []

    header = {column_letter(c.get("r")): cell_value(c) for c in rows[0]}
    records: list[CompanyRecord] = []
    for row in rows[1:]:
        # 按列字母对齐表头 —— 空单元格在 XML 里直接缺席，按顺序读会整行错位。
        data = {header.get(column_letter(c.get("r")), ""): cell_value(c) for c in row}
        data.pop("", None)
        if record := from_row(data, source, signal_date):
            records.append(record)
    return records


def load_table(path: Path, source: str, signal_date: str) -> list[CompanyRecord]:
    """按扩展名分派。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path, source, signal_date)
    if suffix == ".xlsx":
        return load_xlsx(path, source, signal_date)
    if suffix == ".xls":
        raise SystemExit(
            f"{path.name} 是老式 .xls 二进制格式，本仓零依赖读不了。\n"
            "请用 Excel/Numbers 另存为 .xlsx 或 .csv 后重试。"
        )
    raise SystemExit(f"不支持的文件类型：{path.suffix}（支持 .csv / .xlsx）")


# 名录里「有无某资质」普遍用 Y/N 单列表示（广交会有 11 列这种）。逐列建模没完没了，
# 用通用规则识别：值为 Y/是/1 时把列名本身变成 tag。
_YES = {"y", "yes", "是", "1", "true", "√"}
_NO = {"n", "no", "否", "0", "false", "", "-"}

# 企业规模列多为区间文本（"少于50人" / "50~150" / "150~500" / "1000人以上"）。
# ICP 打分要的是数值，取区间下界偏保守 —— 宁可低估规模，也不要把小厂算成大厂
# 而错失（20-300 人是甜区，低估只会让它更靠近甜区，高估会直接踢出）。
def parse_staff_size(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        return 0
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if not nums:
        return 0
    if "少于" in text or "以下" in text or "<" in text:
        return max(nums[0] // 2, 1)
    if "以上" in text or ">" in text:
        return nums[0]
    return nums[0]  # 区间取下界


def tag_slug(text: str) -> str:
    """列名/取值 → tag。去掉空白与常见修饰，保持人类可读（trade-crm 里要给人看）。"""
    slug = re.sub(r"\s+", "", (text or "").strip())
    return re.sub(r"[（）()\[\]【】]", "", slug)


def split_category(booth: str) -> str:
    """广交会摊位号形如「办公文具|第3期|10.3A01-03」，第一段是品类。

    品类是销售分工的核心维度，必须能筛 —— 而 trade-crm 只有 tags 可筛。
    """
    if "|" in booth:
        first = booth.split("|", 1)[0].strip()
        if first and len(first) <= 20:
            return first
    return ""


def from_row(row: dict[str, str], source: str, signal_date: str) -> CompanyRecord | None:
    """一行名录 → CompanyRecord。公司名为空的行直接跳过（表头残留、小计行）。

    未建模的列一律兜进 `extra`，Y/N 资质列转成 `tags` —— 名录字段多且各源不同，
    漏字段是不可逆的（重导数万条代价极高）。
    """
    company = _pick(row, "company")
    if not company:
        return None

    booth = _pick(row, "booth")
    category = split_category(booth)
    province = _pick(row, "province")
    note = _pick(row, "note")

    tags: list[str] = []
    extra: dict[str, str] = {}
    # 只把「实际被选中的那一列」记为已消费（见 _pick_key 的说明）
    consumed = {
        re.sub(r"\s+", "", key).lower()
        for semantic in COLUMN_ALIASES
        if (key := _pick_key(row, semantic))
    }

    for key, value in row.items():
        name = (key or "").strip()
        text = (value or "").strip()
        if not name or not text:
            continue
        low = text.lower()
        if low in _YES:
            tags.append(tag_slug(name))  # 资质列：列名即语义（多届参展/高新展商…）
            continue
        if low in _NO:
            continue
        if re.sub(r"\s+", "", name).lower() in consumed:
            continue
        extra[tag_slug(name)] = text[:200]

    # 品类与省份也进 tags：trade-crm 只有 tags 可筛，销售按品类分工、按属地分配。
    if category:
        tags.append(category)
    if province:
        tags.append(province)
    # 企业类型（生产/外贸/工贸）决定销售打法，值本身就是分类维度
    for label in ("企业类型", "企业性质"):
        if v := extra.get(label):
            tags.append(tag_slug(v))

    return CompanyRecord(
        company=company.strip(),
        source=source,
        signal_date=signal_date,
        phone=normalize_phone(_pick(row, "phone")),
        email=_pick(row, "email").lower(),
        website=normalize_website(_pick(row, "website")),
        contact=_pick(row, "contact"),
        province=province,
        industry=_pick(row, "industry"),
        booth=booth,
        credit_code=_pick(row, "credit_code"),
        staff_size=parse_staff_size(_pick(row, "staff_size")),
        category=category,
        note=note,
        tags=sorted(set(t for t in tags if t)),
        extra=extra,
        raw=dict(row),
    )


def load_csv(path: Path, source: str, signal_date: str) -> list[CompanyRecord]:
    """读名录 CSV。BOM 常见于 Excel 导出，用 utf-8-sig 吃掉。"""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    records = [from_row(row, source, signal_date) for row in rows]
    return [r for r in records if r is not None]


def dedupe(records: list[CompanyRecord]) -> list[CompanyRecord]:
    """同一批里按 request_id 去重，保留信息最全的一条。

    网关也会按 request_id 去重，但那是「第二条被拒」；这里先合并可以避免
    把同一家公司的残缺行推上去、把完整行挤掉。
    """
    best: dict[str, CompanyRecord] = {}
    for record in records:
        key = record.request_id
        if key not in best or _completeness(record) > _completeness(best[key]):
            best[key] = record
    return list(best.values())


def _completeness(record: CompanyRecord) -> int:
    return sum(
        bool(getattr(record, f))
        for f in ("phone", "email", "website", "contact", "credit_code", "industry")
    )

