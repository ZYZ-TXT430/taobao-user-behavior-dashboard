"""分块清洗淘宝用户行为 CSV，并以事务方式写入 DuckDB。"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


# 淘宝 UserBehavior 数据集的字段顺序固定为这五列；输出表会附加时间维度。
FIELDS = ("user_id", "item_id", "category_id", "behavior", "timestamp")
MIN_TIMESTAMP = 946684800  # 2000-01-01 UTC，过滤明显不合理的历史时间。
MAX_INT64 = 2**63 - 1

# 同时识别数据集常见英文列名和中文列名，表头只用于确定字段位置。
HEADER_ALIASES = {
    "user_id": {"user_id", "userid", "user", "uid", "用户id", "用户编号"},
    "item_id": {"item_id", "itemid", "item", "商品id", "商品编号"},
    "category_id": {
        "category_id", "categoryid", "category", "商品类目id", "类目id", "类目编号"
    },
    "behavior": {"behavior", "behavior_type", "行为", "行为类型"},
    "timestamp": {"timestamp", "time", "unix_time", "时间戳", "时间"},
}
VALID_BEHAVIORS = {"pv", "fav", "cart", "buy"}


def detect_header(row: Sequence[str]) -> Mapping[str, int] | None:
    """若首行是可识别表头，返回字段到列下标的映射；无表头时返回 None。"""
    normalized = [value.strip().lower().replace(" ", "") for value in row]
    matches: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        positions = [index for index, value in enumerate(normalized) if value in aliases]
        if positions:
            matches[field] = positions[0]

    # 无表头数据首行是数值，表头至少命中两个已知字段才会触发。
    if len(matches) < 2:
        return None
    missing = set(FIELDS) - matches.keys()
    if missing:
        raise ValueError(f"CSV 表头缺少必需列：{', '.join(sorted(missing))}")
    if len(set(matches.values())) != len(FIELDS):
        raise ValueError("CSV 表头中存在重复字段映射")
    return matches


def normalize_record(
    row: Sequence[str],
    indexes: Mapping[str, int] | None = None,
    now_epoch: int | None = None,
) -> tuple[int, int, int, str, datetime, int, int, int, int]:
    """校验一行数据并转换为 DuckDB 的九列记录；无效输入抛出可读原因。"""
    if indexes is None:
        indexes = {field: index for index, field in enumerate(FIELDS)}
    if len(row) != len(FIELDS) or max(indexes.values()) >= len(row):
        raise ValueError("每条记录必须恰好包含五列")

    try:
        user_id, item_id, category_id = (
            int(row[indexes[field]].strip())
            for field in ("user_id", "item_id", "category_id")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("ID 不是整数") from exc
    if any(value <= 0 or value > MAX_INT64 for value in (user_id, item_id, category_id)):
        raise ValueError("ID 必须是正的 64 位整数")

    behavior = row[indexes["behavior"]].strip().lower()
    if behavior not in VALID_BEHAVIORS:
        raise ValueError("行为类型必须为 pv/fav/cart/buy")

    try:
        epoch = int(row[indexes["timestamp"]].strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("时间戳不是 Unix 秒整数") from exc
    latest_allowed = (int(time.time()) if now_epoch is None else now_epoch) + 86400
    if epoch < MIN_TIMESTAMP or epoch > latest_allowed:
        raise ValueError("时间戳超出合理范围")
    try:
        event_time = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("时间戳无法转换为日期时间") from exc

    return (
        user_id,
        item_id,
        category_id,
        behavior,
        event_time,
        event_time.year,
        event_time.month,
        event_time.day,
        event_time.hour,
    )


def iter_chunks(
    rows: Iterable[Sequence[str]],
    chunk_size: int,
    indexes: Mapping[str, int] | None = None,
    now_epoch: int | None = None,
) -> tuple[Iterable[list[tuple]], list[int]]:
    """将 CSV 行转换为有界批次；第二个返回值是无效记录数的单元素计数器。"""
    invalid_count = [0]

    def batches() -> Iterable[list[tuple]]:
        batch: list[tuple] = []
        for row in rows:
            try:
                batch.append(normalize_record(row, indexes, now_epoch))
            except ValueError:
                invalid_count[0] += 1
            if len(batch) >= chunk_size:
                yield batch
                batch = []
        if batch:
            yield batch

    return batches(), invalid_count


def preprocess(input_path: Path, db_path: Path, chunk_size: int) -> dict[str, int]:
    """分块写入事务暂存表，成功后原子替换正式表，失败则回滚。"""
    if chunk_size <= 0:
        raise ValueError("--chunk-size 必须是正整数")
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到输入 CSV：{input_path}")

    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("缺少 DuckDB，请先运行：pip install -r requirements.txt") from exc
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("缺少 pandas，请先运行：pip install -r requirements.txt") from exc

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path))
    transaction_open = False
    accepted_count = 0
    invalid_count = 0
    try:
        connection.execute("BEGIN TRANSACTION")
        transaction_open = True
        connection.execute(
            """
            CREATE TEMP TABLE _user_behavior_stage (
                user_id BIGINT,
                item_id BIGINT,
                category_id BIGINT,
                behavior VARCHAR,
                "timestamp" TIMESTAMP,
                year INTEGER,
                month INTEGER,
                day INTEGER,
                hour INTEGER
            )
            """
        )

        # newline="" 交给 csv 模块处理引号和跨行字段，utf-8-sig 兼容 BOM。
        with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.reader(csv_file)
            first_row = next(reader, None)
            if first_row is None:
                raise ValueError("输入 CSV 是空文件")
            indexes = detect_header(first_row)
            source_rows = reader if indexes is not None else _prepend(first_row, reader)
            batches, invalid_counter = iter_chunks(
                source_rows, chunk_size, indexes, now_epoch=int(time.time())
            )
            for batch in batches:
                # DuckDB 直接扫描 Pandas 批次，避免逐行 executemany 的高开销。
                frame = pd.DataFrame(
                    batch,
                    columns=[
                        "user_id", "item_id", "category_id", "behavior",
                        "timestamp", "year", "month", "day", "hour",
                    ],
                )
                connection.register("_user_behavior_batch", frame)
                try:
                    connection.execute(
                        "INSERT INTO _user_behavior_stage SELECT * FROM _user_behavior_batch"
                    )
                finally:
                    connection.unregister("_user_behavior_batch")
                accepted_count += len(batch)
            invalid_count = invalid_counter[0]

        if accepted_count == 0:
            raise ValueError("CSV 中没有可导入的有效记录；为保护旧数据，本次导入已取消")

        # 正式表只在所有输入批次均已成功读取、转换和写入后才替换。
        connection.execute(
            """
            CREATE OR REPLACE TABLE user_behavior AS
            SELECT DISTINCT user_id, item_id, category_id, behavior,
                   "timestamp", year, month, day, hour
            FROM _user_behavior_stage
            """
        )
        stats = connection.execute(
            """
            SELECT COUNT(*) AS records,
                   COUNT(DISTINCT user_id) AS users,
                   COUNT(DISTINCT item_id) AS items
            FROM user_behavior
            """
        ).fetchone()
        connection.execute("COMMIT")
        transaction_open = False
    except Exception:
        if transaction_open:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    records, users, items = (int(value) for value in stats)
    return {
        "records": records,
        "users": users,
        "items": items,
        "accepted_before_dedup": accepted_count,
        "invalid": invalid_count,
        "duplicates": accepted_count - records,
    }


def _prepend(first_row: Sequence[str], remaining: Iterable[Sequence[str]]) -> Iterable[Sequence[str]]:
    """把已读取的首行放回无表头 CSV 的数据流。"""
    yield first_row
    yield from remaining


def main() -> int:
    default_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="分块清洗淘宝 UserBehavior.csv，并写入 DuckDB user_behavior 表。"
    )
    parser.add_argument(
        "--input", type=Path, default=default_dir / "UserBehavior.csv",
        help="淘宝行为 CSV 路径（默认：脚本同目录 UserBehavior.csv）",
    )
    parser.add_argument(
        "--db", type=Path, default=default_dir / "user_behavior.duckdb",
        help="DuckDB 文件路径（默认：脚本同目录 user_behavior.duckdb）",
    )
    parser.add_argument("--chunk-size", type=int, default=100_000, help="每批处理行数")
    args = parser.parse_args()

    try:
        stats = preprocess(args.input, args.db, args.chunk_size)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"数据处理失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"数据处理失败，数据库事务已回滚：{exc}", file=sys.stderr)
        return 1

    print("数据处理完成")
    print(f"有效唯一记录数：{stats['records']:,}")
    print(f"用户数：{stats['users']:,}")
    print(f"商品数：{stats['items']:,}")
    print(f"重复记录数：{stats['duplicates']:,}")
    print(f"无效记录数：{stats['invalid']:,}")
    print(f"数据库：{args.db.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
