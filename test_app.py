"""仪表盘图表、筛选和只读 SQL 校验的回归测试。"""

import unittest
from datetime import date, datetime

import pandas as pd

from app import build_category_chart, filter_parameters, validate_readonly_sql


class CategoryChartTests(unittest.TestCase):
    def test_numeric_category_ids_use_labeled_categorical_axis(self):
        categories = pd.DataFrame(
            {
                "category_id": [9001, 2002, 7003],
                "events": [8, 24, 16],
                "pv": [6, 20, 12],
                "favorites": [1, 1, 1],
                "carts": [1, 2, 2],
                "orders": [0, 1, 1],
            }
        )

        chart = build_category_chart(categories)

        self.assertEqual(chart.layout.yaxis.type, "category")
        self.assertEqual(list(chart.layout.yaxis.categoryarray), ["9001", "7003", "2002"])
        self.assertEqual(list(chart.data[0].y), ["2002", "7003", "9001"])
        self.assertEqual(chart.data[0].textposition, "outside")
        self.assertGreaterEqual(chart.layout.margin.l, 100)
        self.assertGreaterEqual(chart.layout.height, 420)


class ReadonlySqlTests(unittest.TestCase):
    def test_allows_select_and_rewrites_table_to_filtered_cte(self):
        safe_query, error = validate_readonly_sql(
            "WITH daily AS (SELECT day, COUNT(*) AS n "
            "FROM user_behavior GROUP BY day) SELECT * FROM daily"
        )

        self.assertIsNone(error)
        self.assertIn("FROM _selected_behavior", safe_query)

    def test_blocks_multi_statement_comments_writes_and_other_tables(self):
        unsafe_queries = (
            "SELECT * FROM user_behavior; DROP TABLE user_behavior",
            "SELECT * FROM user_behavior -- bypass filters",
            "DELETE FROM user_behavior",
            "SELECT * FROM private_table",
        )
        for query in unsafe_queries:
            with self.subTest(query=query):
                safe_query, error = validate_readonly_sql(query)
                self.assertIsNone(safe_query)
                self.assertTrue(error)

    def test_blocks_external_file_read_functions(self):
        safe_query, error = validate_readonly_sql(
            "SELECT * FROM read_csv_auto('private.csv')"
        )

        self.assertIsNone(safe_query)
        self.assertIn("user_behavior", error)


class DateFilterTests(unittest.TestCase):
    def test_date_filter_uses_native_datetime_and_inclusive_end_date(self):
        parameters = filter_parameters(date(2026, 9, 1), date(2026, 9, 3), 2, 22)

        self.assertEqual(
            parameters,
            (
                datetime(2026, 9, 1, 0, 0),
                datetime(2026, 9, 4, 0, 0),
                2,
                22,
            ),
        )


if __name__ == "__main__":
    unittest.main()
