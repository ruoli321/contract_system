# ═══════════════════════════════════════════════════════════════════
# M22 · OCR 块级置信度透传 单元测试
#
# 覆盖：
#   1. merge_block_stats          — 多页统计累加
#   2. attach_ocr_confidence      — 聚合进 quality（ratio/排序/截断/空防御）
#   3. _paddleocr_run             — 三级分流（采信/低置信靶点/丢弃标记）
#      （用 FakeOCR 模拟 3.x predict()，不需要真实 paddle 依赖）
#   4. build_quality              — low_conf → review_reasons 靶点/强提醒
# ═══════════════════════════════════════════════════════════════════
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.services.pdf_parser import (
    LOW_CONF_BLOCKS_MAX,
    PdfProcessor,
    attach_ocr_confidence,
    merge_block_stats,
)
from app.services.gatekeeper import build_quality, LOW_CONF_RATIO_STRICT
from app.services.validator import ValidationReport


# ─────────────────────────────────────────────────────────────────────
# 1. merge_block_stats
# ─────────────────────────────────────────────────────────────────────
class TestMergeBlockStats(unittest.TestCase):

    def test_merge_accumulates_total_and_low_conf(self):
        acc = {}
        merge_block_stats(acc, {"total": 10, "low_conf": [{"page": 1, "score": 0.6}]})
        merge_block_stats(acc, {"total": 8, "low_conf": [{"page": 2, "score": 0.7}]})
        self.assertEqual(acc["total"], 18)
        self.assertEqual(len(acc["low_conf"]), 2)

    def test_merge_ignores_empty(self):
        acc = {}
        merge_block_stats(acc, None)
        merge_block_stats(acc, {})
        self.assertEqual(acc, {})


# ─────────────────────────────────────────────────────────────────────
# 2. attach_ocr_confidence
# ─────────────────────────────────────────────────────────────────────
class TestAttachOcrConfidence(unittest.TestCase):

    def test_attaches_ratio_and_blocks(self):
        q = {"score": 0.9, "ok": True}
        stats = {"total": 100, "low_conf": [{"page": 1, "text": "abc", "score": 0.6, "dropped": False}]}
        attach_ocr_confidence(q, stats)
        self.assertEqual(q["ocr_total_blocks"], 100)
        self.assertEqual(q["low_conf_ratio"], 0.01)
        self.assertEqual(len(q["low_conf_blocks"]), 1)

    def test_dropped_blocks_sorted_first(self):
        q = {"score": 0.9}
        stats = {"total": 10, "low_conf": [
            {"page": 1, "text": "低置信进文本", "score": 0.7, "dropped": False},
            {"page": 1, "text": "被丢弃块", "score": 0.4, "dropped": True},
        ]}
        attach_ocr_confidence(q, stats)
        self.assertTrue(q["low_conf_blocks"][0]["dropped"])  # 丢弃块风险最高排最前

    def test_blocks_truncated_to_max(self):
        q = {"score": 0.9}
        stats = {"total": 100, "low_conf": [
            {"page": 1, "text": f"块{i}", "score": 0.6, "dropped": False}
            for i in range(50)
        ]}
        attach_ocr_confidence(q, stats)
        self.assertEqual(len(q["low_conf_blocks"]), LOW_CONF_BLOCKS_MAX)

    def test_no_total_leaves_quality_untouched(self):
        q = {"score": 0.9}
        attach_ocr_confidence(q, {"total": 0, "low_conf": [{"page": 1}]})
        self.assertNotIn("low_conf_ratio", q)

    def test_none_inputs_defensive(self):
        self.assertIsNone(attach_ocr_confidence(None, {"total": 1, "low_conf": []}))
        q = {"score": 0.9}
        self.assertEqual(attach_ocr_confidence(q, None), q)


# ─────────────────────────────────────────────────────────────────────
# 3. _paddleocr_run 三级分流（FakeOCR 模拟 3.x predict）
# ─────────────────────────────────────────────────────────────────────
class FakeOcrResult(dict):
    """模拟 PaddleOCR 3.x 的 OCRResult（dict 访问 rec_texts/rec_scores）"""


class FakeOCR:
    """模拟 PaddleOCR 3.x 引擎"""

    def __init__(self, texts, scores):
        self._res = [FakeOcrResult(rec_texts=texts, rec_scores=scores)]

    def predict(self, img):
        return self._res


class TestPaddleOcrRun(unittest.TestCase):

    def _processor_with_fake(self, texts, scores):
        p = PdfProcessor(ocr_engine="paddleocr", enable_table_detect=False)
        p._ocr = FakeOCR(texts, scores)
        return p

    def test_three_tier_routing(self):
        # 0.95 采信 / 0.62 低置信靶点（进文本）/ 0.30 丢弃并标记 dropped
        p = self._processor_with_fake(
            ["金额：壹佰贰拾万元整", "签订时间：2026年3月15日", "模糊不清的一行"],
            [0.95, 0.62, 0.30],
        )
        text, tables, stats = p._paddleocr_run("fake_img", page_num=3)

        self.assertIn("壹佰贰拾万元整", text)
        self.assertIn("2026年3月15日", text)
        self.assertNotIn("模糊不清", text)  # dropped 不进文本

        self.assertEqual(stats["total"], 3)
        self.assertEqual(len(stats["low_conf"]), 2)
        dropped = [b for b in stats["low_conf"] if b["dropped"]]
        kept = [b for b in stats["low_conf"] if not b["dropped"]]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["page"], 3)
        self.assertEqual(dropped[0]["score"], 0.3)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["score"], 0.62)

    def test_high_confidence_no_targets(self):
        p = self._processor_with_fake(["甲公司", "乙公司"], [0.99, 0.98])
        _, _, stats = p._paddleocr_run("fake_img", page_num=1)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["low_conf"], [])

    def test_no_engine_returns_empty_stats(self):
        p = PdfProcessor(ocr_engine="paddleocr", enable_table_detect=False)
        text, tables, stats = p._paddleocr_run("fake_img", page_num=1)
        self.assertEqual(text, "")
        self.assertEqual(tables, [])
        self.assertEqual(stats, {"total": 0, "low_conf": []})

    def test_engine_exception_graceful(self):
        class BrokenOCR:
            def predict(self, img):
                raise RuntimeError("boom")
        p = PdfProcessor(ocr_engine="paddleocr", enable_table_detect=False)
        p._ocr = BrokenOCR()
        text, _, stats = p._paddleocr_run("fake_img", page_num=1)
        self.assertEqual(text, "")
        self.assertEqual(stats["total"], 0)


# ─────────────────────────────────────────────────────────────────────
# 4. build_quality — low_conf → review_reasons
# ─────────────────────────────────────────────────────────────────────
def _report(conf=0.95, **kw):
    return ValidationReport(system_confidence=conf, **kw)


class TestGatekeeperLowConf(unittest.TestCase):

    def test_low_conf_blocks_force_review_with_target(self):
        q = build_quality(
            _report(),
            parse_quality={"score": 0.9, "ok": True, "low_conf_ratio": 0.05,
                           "ocr_total_blocks": 100,
                           "low_conf_blocks": [{"page": 3, "text": "签订时间：2026年3月15日",
                                                "score": 0.62, "dropped": False}]},
        )
        self.assertTrue(q["needs_review"])
        ocr_reasons = [r for r in q["review_reasons"] if "OCR" in r]
        self.assertEqual(len(ocr_reasons), 1)
        self.assertIn("OCR 低置信靶点", ocr_reasons[0])
        self.assertIn("页码 [3]", ocr_reasons[0])
        self.assertIn("0.62", ocr_reasons[0])

    def test_dropped_blocks_mention_content_missing(self):
        q = build_quality(
            _report(),
            parse_quality={"score": 0.85, "ok": True, "low_conf_ratio": 0.08,
                           "ocr_total_blocks": 50,
                           "low_conf_blocks": [{"page": 1, "text": "盖堂", "score": 0.32,
                                                "dropped": True}]},
        )
        reason = [r for r in q["review_reasons"] if "OCR" in r][0]
        self.assertIn("已被丢弃（内容缺失）", reason)

    def test_high_ratio_gets_strong_warning(self):
        q = build_quality(
            _report(),
            parse_quality={"score": 0.9, "ok": True, "low_conf_ratio": LOW_CONF_RATIO_STRICT + 0.1,
                           "ocr_total_blocks": 20,
                           "low_conf_blocks": [{"page": 2, "text": "xxx", "score": 0.5,
                                                "dropped": False}]},
        )
        reason = [r for r in q["review_reasons"] if "OCR" in r][0]
        self.assertIn("OCR 识别可靠性差", reason)

    def test_no_low_conf_no_ocr_reason(self):
        q = build_quality(_report(), parse_quality={"score": 0.95, "ok": True})
        self.assertFalse(any("OCR" in r for r in q["review_reasons"]))
        self.assertFalse(q["needs_review"])

    def test_parse_quality_none_untouched(self):
        q = build_quality(_report(), parse_quality=None)
        self.assertFalse(q["needs_review"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
