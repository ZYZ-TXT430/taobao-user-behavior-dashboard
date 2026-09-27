"""数据预处理纯 Python 逻辑的轻量回归测试。"""

import unittest
from datetime import datetime

from data_preprocess import detect_header, iter_chunks, normalize_record


class NormalizeRecordTests(unittest.TestCase):
    def test_unheaded_taobao_record_has_utc_dimensions(self):
        record = normalize_record(
            ["42", "77", "9", "PV", "1512000000"], now_epoch=1600000000
        )
        self.assertEqual(record[:4], (42, 77, 9, "pv"))
        self.assertEqual(record[4], datetime(2017, 11, 30, 0, 0))
        self.assertEqual(record[5:], (2017, 11, 30, 0))

    def test_header_mapping_accepts_chinese_names_and_reordered_columns(self):
        indexes = detect_header(["行为类型", "用户ID", "时间戳", "商品ID", "商品类目ID"])
        self.assertEqual(
            indexes,
            {"user_id": 1, "item_id": 3, "category_id": 4, "behavior": 0, "timestamp": 2},
        )
        record = normalize_record(
            ["buy", "42", "1512000000", "77", "9"], indexes, now_epoch=1600000000
        )
        self.assertEqual(record[:4], (42, 77, 9, "buy"))

    def test_rejects_invalid_ids_behaviors_and_timestamps(self):
        base = ["42", "77", "9", "pv", "1512000000"]
        for field, value in ((0, "0"), (3, "share"), (4, "not-a-time")):
            row = list(base)
            row[field] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_record(row, now_epoch=1600000000)

    def test_chunking_skips_bad_rows_without_loading_extra_batches(self):
        rows = [
            ["1", "2", "3", "pv", "1512000000"],
            ["0", "2", "3", "pv", "1512000000"],
            ["4", "5", "6", "buy", "1512000001"],
        ]
        batches, invalid = iter_chunks(rows, 1, now_epoch=1600000000)
        self.assertEqual([len(batch) for batch in batches], [1, 1])
        self.assertEqual(invalid[0], 1)

    def test_rejects_incomplete_recognized_header(self):
        with self.assertRaisesRegex(ValueError, "缺少必需列"):
            detect_header(["user_id", "item_id", "time", "quantity", "behavior"])


if __name__ == "__main__":
    unittest.main()
