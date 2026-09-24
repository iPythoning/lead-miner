"""email_clean 单测：语法/MX/SMTP 分级与 verify_records 集成。"""
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
from miner.email_clean import classify_email, clean_email, verify_records
from miner.records import CompanyRecord


def rec(**kw) -> CompanyRecord:
    base = dict(company="测试公司", source="test-source", signal_date="2026-08-17")
    base.update(kw)
    return CompanyRecord(**base)


class TestCleanEmail(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(clean_email("  Foo@Bar.COM "), "foo@bar.com")
        self.assertEqual(clean_email("a@b.com；c@d.com"), "a@b.com")
        self.assertEqual(clean_email("x@y.cn"), "x@y.cn")
        self.assertEqual(clean_email("not-an-email"), "")
        self.assertEqual(clean_email(""), "")

    def test_bad_domain(self):
        self.assertEqual(classify_email("a@example.com", smtp=False), "invalid")

    @patch("miner.email_clean.has_mx", return_value=False)
    def test_no_mx(self, _):
        self.assertEqual(classify_email("a@nodomain.xyz", smtp=False), "invalid")

    @patch("miner.email_clean.has_mx", return_value=True)
    def test_valid_mx_no_smtp(self, _):
        self.assertEqual(classify_email("a@real-domain.com", smtp=False), "valid_mx")

    @patch("miner.email_clean.has_mx", return_value=True)
    @patch("miner.email_clean.smtp_check", return_value="verified")
    def test_smtp_verified(self, _m, _h):
        self.assertEqual(classify_email("a@real.com", smtp=True), "verified")

    @patch("miner.email_clean.has_mx", return_value=True)
    @patch("miner.email_clean.smtp_check", return_value="unknown")
    def test_smtp_unknown_degrades(self, _m, _h):
        # 反探测/网络问题 → unknown 而不是 invalid（宁可放过）
        self.assertEqual(classify_email("a@real.com", smtp=True), "unknown")


class TestVerifyRecords(unittest.TestCase):
    def test_pipeline_integration(self):
        records = [
            rec(company="A", email="foo@example.com"),      # invalid（example.com 假域名）
            rec(company="B", email=""),                      # none
            rec(company="C", email="bad-email"),             # invalid（语法错）
            rec(company="D", email="sales@real-company.com"),  # verified（mock SMTP）
        ]
        with patch("miner.email_clean.has_mx", return_value=True), \
             patch("miner.email_clean.smtp_check", return_value="verified"):
            stats = verify_records(records, smtp=True)
        self.assertEqual(stats["invalid"], 2)   # example.com + 语法错
        self.assertEqual(stats["none"], 1)
        self.assertEqual(stats["verified"], 1)  # 正常邮箱
        self.assertEqual(records[0].extra["email_status"], "invalid")
        self.assertEqual(records[1].extra["email_status"], "none")
        self.assertEqual(records[2].extra["email_status"], "invalid")
        self.assertEqual(records[3].extra["email_status"], "verified")


if __name__ == "__main__":
    unittest.main()
