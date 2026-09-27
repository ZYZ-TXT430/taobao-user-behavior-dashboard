"""淘宝用户行为交互式分析仪表盘。"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sqlglot
from sqlglot import exp
import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "user_behavior.duckdb"
MAX_SQL_ROWS = 1_000
COLORS = {
    "ink": "#172A45",
    "muted": "#65758B",
    "blue": "#2864DC",
    "cyan": "#18A7A1",
    "amber": "#E69B29",
    "coral": "#E46E5B",
    "violet": "#8063CF",
    "grid": "#E6EBF2",
    "paper": "#FFFFFF",
}
BEHAVIOR_LABELS = {"pv": "浏览", "fav": "收藏", "cart": "加购", "buy": "下单"}
SEGMENT_ORDER = ["高价值", "潜力", "新", "流失"]


def _database_fingerprint(db_path: str) -> tuple[int, int]:
    stat = os.stat(db_path)
    return stat.st_mtime_ns, stat.st_size


@st.cache_data(ttl=300, show_spinner=False)
def get_date_bounds(db_path: str, fingerprint: tuple[int, int]) -> tuple[Any, Any, int]:
    """读取数据时间边界；使用数据库只读连接。"""
    del fingerprint
    with duckdb.connect(db_path, read_only=True) as connection:
        row = connection.execute(
            'SELECT MIN("timestamp"), MAX("timestamp"), COUNT(*) FROM user_behavior'
        ).fetchone()
    return row[0], row[1], int(row[2])


@st.cache_data(ttl=300, show_spinner=False)
def query_frame(
    db_path: str,
    fingerprint: tuple[int, int],
    sql: str,
    parameters: tuple[Any, ...] = (),
) -> pd.DataFrame:
    """执行内部分析 SQL 并缓存结果；参数通过 DuckDB 占位符绑定。"""
    del fingerprint
    with duckdb.connect(db_path, read_only=True) as connection:
        return connection.execute(sql, list(parameters)).fetchdf()


def filter_parameters(
    start_date: date, end_date: date, start_hour: int, end_hour: int
) -> tuple[datetime, datetime, int, int]:
    """统一生成全页时间筛选的半开时间区间和小时范围参数。"""
    start = datetime.combine(start_date, time.min)
    end = datetime.combine(end_date + timedelta(days=1), time.min)
    return start, end, int(start_hour), int(end_hour)


def where_clause() -> str:
    return '"timestamp" >= ? AND "timestamp" < ? AND hour BETWEEN ? AND ?'


def plotly_layout(fig: go.Figure, height: int = 340) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, 'Microsoft YaHei', sans-serif", color=COLORS["ink"], size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hoverlabel=dict(bgcolor=COLORS["ink"], font_color="white"),
    )
    fig.update_xaxes(showgrid=False, linecolor=COLORS["grid"], zeroline=False)
    fig.update_yaxes(gridcolor=COLORS["grid"], zeroline=False, rangemode="tozero")
    return fig


def build_category_chart(categories: pd.DataFrame) -> go.Figure:
    """构建标签可读、按行为量排序的类目排行横向条形图。"""
    ranked = categories.sort_values("events", ascending=False).copy()
    ranked["category_id"] = ranked["category_id"].astype(str)
    # Plotly 会把纯数字类目 ID 误判为连续数值轴，显式设为分类轴才能显示每个类目。
    category_order = ranked.sort_values("events")["category_id"].tolist()
    figure = px.bar(
        ranked,
        x="events",
        y="category_id",
        orientation="h",
        text="events",
        category_orders={"category_id": category_order},
        labels={"events": "行为事件数", "category_id": "商品类目 ID"},
        color_discrete_sequence=[COLORS["blue"]],
        hover_data={"events": ":,", "category_id": True},
    )
    figure.update_traces(
        texttemplate="%{x:,}",
        textposition="outside",
        cliponaxis=False,
        marker_line_width=0,
        hovertemplate="类目 %{y}<br>行为事件：%{x:,}<extra></extra>",
    )
    figure.update_layout(
        showlegend=False,
        margin=dict(l=104, r=52, t=18, b=46),
        bargap=0.34,
    )
    figure.update_xaxes(
        title_text="行为事件数",
        range=[0, int(ranked["events"].max() * 1.2)],
        tickformat=",",
        showgrid=True,
    )
    figure.update_yaxes(
        type="category",
        title_text="商品类目 ID",
        categoryorder="array",
        categoryarray=category_order,
        automargin=True,
        showgrid=False,
        rangemode=None,
    )
    figure = plotly_layout(figure, max(420, 38 * len(ranked) + 76))
    figure.update_layout(margin=dict(l=104, r=52, t=18, b=46))
    return figure


def metric_query(db_path: str, fingerprint: tuple[int, int], params: tuple[Any, ...]) -> dict[str, Any]:
    query = f"""
        SELECT
            COUNT(*) FILTER (WHERE behavior = 'pv') AS pv,
            COUNT(DISTINCT user_id) FILTER (WHERE behavior = 'pv') AS browse_uv,
            COUNT(DISTINCT user_id) FILTER (WHERE behavior = 'buy') AS buyers
        FROM user_behavior
        WHERE {where_clause()}
    """
    frame = query_frame(db_path, fingerprint, query, params)
    return frame.iloc[0].to_dict()


def build_rfm_query(params: tuple[Any, ...], preview: bool = False) -> tuple[str, tuple[Any, ...]]:
    """按筛选范围聚合用户 R/F/M；R 以筛选结果中最后一次行为为参照。"""
    statement = f"""
        WITH filtered AS (
            SELECT user_id, behavior, "timestamp"
            FROM user_behavior
            WHERE {where_clause()}
        ),
        per_user AS (
            SELECT user_id,
                   DATE_DIFF('day', MAX("timestamp"), (SELECT MAX("timestamp") FROM filtered)) AS recency_days,
                   COUNT(*) AS frequency,
                   COUNT(*) FILTER (WHERE behavior = 'buy') AS orders
            FROM filtered
            GROUP BY user_id
        ),
        thresholds AS (
            SELECT
                QUANTILE_CONT(recency_days, 0.50) AS r50,
                QUANTILE_CONT(recency_days, 0.75) AS r75,
                QUANTILE_CONT(frequency, 0.50) AS f50,
                QUANTILE_CONT(frequency, 0.75) AS f75
            FROM per_user
        ),
        segmented AS (
            SELECT per_user.*,
                   CASE
                     WHEN recency_days > r75 THEN '流失'
                     WHEN recency_days <= r50 AND frequency >= f75 AND orders > 0 THEN '高价值'
                     WHEN frequency <= 1 AND orders = 0 THEN '新'
                     ELSE '潜力'
                   END AS segment
            FROM per_user CROSS JOIN thresholds
        )
    """
    if preview:
        statement += """
            SELECT user_id, recency_days, frequency, orders, segment
            FROM segmented
            ORDER BY CASE segment
                       WHEN '高价值' THEN 1 WHEN '潜力' THEN 2
                       WHEN '新' THEN 3 ELSE 4
                     END, orders DESC, frequency DESC, recency_days ASC
            LIMIT 100
        """
    else:
        statement += """
            SELECT segment, COUNT(*) AS users,
                   ROUND(AVG(recency_days), 1) AS avg_recency_days,
                   ROUND(AVG(frequency), 1) AS avg_frequency,
                   ROUND(AVG(orders), 1) AS avg_orders
            FROM segmented
            GROUP BY segment
        """
    return statement, params


def _sql_functions(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for node in tree.find_all(exp.Func):
        try:
            name = node.name or node.sql_name()
        except (AttributeError, TypeError):
            name = node.name
        if name:
            names.add(str(name).lower())
    return names


def validate_readonly_sql(sql: str) -> tuple[str | None, str | None]:
    """仅允许单个、只读 SELECT/WITH 查询，且数据源只能是 user_behavior。"""
    candidate = sql.strip()
    if not candidate:
        return None, "请输入一条 SELECT 或 WITH 查询。"
    if ";" in candidate:
        return None, "不允许分号或多条 SQL；请只提交一条查询。"
    if re.search(r"--|/\*|\*/|#", candidate):
        return None, "不允许 SQL 注释，请移除注释后重试。"

    try:
        parsed = sqlglot.parse(candidate, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        return None, f"SQL 语法无法解析：{exc}"
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        return None, "仅支持单条 SELECT 或 WITH ... SELECT 查询。"
    tree = parsed[0]

    cte_names = {
        cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name
    }
    if {"user_behavior", "_selected_behavior"} & cte_names:
        return None, "CTE 名称不能覆盖保留数据表名。"
    for table in tree.find_all(exp.Table):
        table_name = table.name.lower()
        if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
            return None, "只允许读取 user_behavior 表及查询内部的 CTE。"
        if table_name != "user_behavior" and table_name not in cte_names:
            return None, f"禁止读取数据表 {table.name!r}；仅开放 user_behavior。"

    blocked_functions = {
        "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json",
        "read_json_auto", "read_json_objects", "read_json_objects_auto",
        "read_ndjson", "read_ndjson_auto", "read_avro", "read_orc", "read_arrow",
        "read_ipc", "read_xlsx", "read_excel", "read_fwf", "read_pickle",
        "read_sas", "read_text", "read_blob", "glob", "query", "query_table",
        "sqlite_scan", "postgres_scan", "mysql_scan", "iceberg_scan", "delta_scan",
        "http_get", "http_post", "load_extension", "install_extension",
        "force_checkpoint", "checkpoint", "nextval", "setval",
    }
    unsafe = sorted(_sql_functions(tree) & blocked_functions)
    if unsafe:
        return None, f"查询包含不允许的外部读取或高危函数：{', '.join(unsafe)}。"

    # 把基表重写为受全局日期和小时筛选约束的 CTE，避免 SQL 标签页绕过侧栏过滤。
    for table in tree.find_all(exp.Table):
        if table.name.lower() == "user_behavior":
            table.set("this", exp.to_identifier("_selected_behavior"))
    return tree.sql(dialect="duckdb"), None


def execute_user_query(
    db_path: str,
    fingerprint: tuple[int, int],
    user_sql: str,
    params: tuple[Any, ...],
) -> tuple[pd.DataFrame | None, str | None]:
    safe_sql, validation_error = validate_readonly_sql(user_sql)
    if validation_error:
        return None, validation_error
    bounded_query = f"""
        WITH _selected_behavior AS (
            SELECT * FROM user_behavior WHERE {where_clause()}
        ),
        _user_query AS ({safe_sql})
        SELECT * FROM _user_query
        LIMIT {MAX_SQL_ROWS}
    """
    try:
        return query_frame(db_path, fingerprint, bounded_query, params), None
    except (duckdb.Error, OSError, ValueError) as exc:
        return None, f"查询执行失败：{exc}"


def add_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #F3F6FA; color: #172A45; }
        [data-testid="stSidebar"] { background: #EAF0F8; border-right: 1px solid #DDE5EF; }
        [data-testid="stHeader"] { background: rgba(243,246,250,.9); }
        [data-testid="stMetric"] {
            background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 12px;
            padding: 16px 18px; box-shadow: 0 2px 8px rgba(23,42,69,.035);
        }
        [data-testid="stMetricLabel"] { color: #65758B; }
        [data-testid="stMetricValue"] { color: #172A45; }
        .block-container { max-width: 1440px; padding-top: 2.1rem; padding-bottom: 3rem; }
        div[data-testid="stTabs"] button { font-weight: 650; }
        [data-testid="stAlert"] { border-radius: 10px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_overview(
    db_path: str, fingerprint: tuple[int, int], params: tuple[Any, ...], metrics: dict[str, Any]
) -> None:
    st.subheader("行为趋势与节奏")
    trend_sql = f"""
        SELECT CAST("timestamp" AS DATE) AS event_date,
               COUNT(*) FILTER (WHERE behavior = 'pv') AS pv,
               COUNT(DISTINCT user_id) FILTER (WHERE behavior = 'pv') AS uv
        FROM user_behavior WHERE {where_clause()}
        GROUP BY event_date ORDER BY event_date
    """
    trend = query_frame(db_path, fingerprint, trend_sql, params)
    hourly_sql = f"""
        SELECT hour, COUNT(*) AS events
        FROM user_behavior WHERE behavior = 'pv' AND {where_clause()}
        GROUP BY hour ORDER BY hour
    """
    hourly = query_frame(db_path, fingerprint, hourly_sql, params)
    behavior_sql = f"""
        SELECT behavior, COUNT(*) AS events
        FROM user_behavior WHERE {where_clause()}
        GROUP BY behavior ORDER BY events DESC
    """
    behavior = query_frame(db_path, fingerprint, behavior_sql, params)

    left, right = st.columns([1.65, 1], gap="large")
    with left:
        st.markdown("##### 每日浏览量与独立访客")
        if trend.empty:
            st.info("所选时间范围没有浏览行为。")
        else:
            chart_data = trend.melt(
                id_vars="event_date", value_vars=["pv", "uv"],
                var_name="指标", value_name="人数 / 次数",
            )
            chart_data["指标"] = chart_data["指标"].map({"pv": "PV", "uv": "UV"})
            fig = px.line(
                chart_data, x="event_date", y="人数 / 次数", color="指标",
                color_discrete_map={"PV": COLORS["blue"], "UV": COLORS["cyan"]},
                markers=True,
            )
            fig.update_traces(line_width=2.5, marker_size=6)
            st.plotly_chart(plotly_layout(fig, 360), use_container_width=True)
    with right:
        st.markdown("##### 行为构成")
        if behavior.empty:
            st.info("所选时间范围暂无行为记录。")
        else:
            behavior["行为"] = behavior["behavior"].map(BEHAVIOR_LABELS)
            behavior_total = int(behavior["events"].sum())
            fig = px.pie(
                behavior, names="行为", values="events", hole=0.62,
                color="行为",
                color_discrete_map={
                    "浏览": COLORS["blue"], "收藏": COLORS["violet"],
                    "加购": COLORS["amber"], "下单": COLORS["cyan"],
                },
            )
            fig.update_traces(
                textinfo="none",
                hovertemplate="%{label}<br>行为次数：%{value:,}<br>占比：%{percent}<extra></extra>",
            )
            fig.add_annotation(
                x=0.5,
                y=0.5,
                text=f"<b>{behavior_total:,}</b><br><span style='font-size:12px'>次行为</span>",
                showarrow=False,
                font=dict(size=21, color=COLORS["ink"]),
            )
            fig = plotly_layout(fig, 360)
            fig.update_layout(
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="center",
                    x=0.5,
                    font=dict(size=11),
                ),
                margin=dict(l=16, r=16, t=54, b=16),
            )
            st.plotly_chart(
                fig,
                use_container_width=True,
                config={"displayModeBar": False, "scrollZoom": False},
            )

    st.markdown("##### 一天中的浏览时段")
    if hourly.empty:
        st.info("当前筛选中没有浏览记录。")
    else:
        hour_chart = px.bar(
            hourly, x="hour", y="events",
            labels={"hour": "小时（UTC）", "events": "浏览次数"},
            color_discrete_sequence=[COLORS["blue"]],
        )
        hour_chart.update_traces(marker_line_width=0)
        st.plotly_chart(plotly_layout(hour_chart, 260), use_container_width=True)

    st.subheader("筛选结果洞察")
    pv = int(metrics.get("pv") or 0)
    uv = int(metrics.get("browse_uv") or 0)
    buyers = int(metrics.get("buyers") or 0)
    conversion = buyers / uv if uv else 0
    if hourly.empty and not pv:
        st.info("没有可供分析的浏览数据。请扩大日期或小时范围。")
    else:
        peak_hour = int(hourly.loc[hourly["events"].idxmax(), "hour"]) if not hourly.empty else None
        if peak_hour is not None:
            st.markdown(
                f"- **流量高峰：** UTC {peak_hour:02d}:00 时段浏览最多，建议将活动曝光与客服排班向该时段靠拢。"
            )
        if uv:
            if conversion < 0.02:
                st.markdown(
                    f"- **转化关注：** 浏览 UV 为 {uv:,}，浏览-下单用户转化率为 {conversion:.1%}；"
                    "优先检查商品详情页、库存、价格和结算链路。"
                )
            else:
                st.markdown(
                    f"- **转化表现：** {buyers:,} 位用户下单，浏览-下单用户转化率为 {conversion:.1%}；"
                    "可对高浏览未下单商品开展定向复访。"
                )
        if pv:
            st.caption(
                f"本次筛选共 {pv:,} 次浏览；时段按数据集 Unix 时间戳以 UTC 展示。"
            )


def show_products(
    db_path: str, fingerprint: tuple[int, int], params: tuple[Any, ...]
) -> None:
    st.subheader("类目行为量")
    category_sql = f"""
        SELECT category_id,
               COUNT(*) AS events,
               COUNT(*) FILTER (WHERE behavior = 'pv') AS pv,
               COUNT(*) FILTER (WHERE behavior = 'fav') AS favorites,
               COUNT(*) FILTER (WHERE behavior = 'cart') AS carts,
               COUNT(*) FILTER (WHERE behavior = 'buy') AS orders
        FROM user_behavior WHERE {where_clause()}
        GROUP BY category_id ORDER BY events DESC LIMIT 12
    """
    categories = query_frame(db_path, fingerprint, category_sql, params)
    if categories.empty:
        st.info("当前时间范围没有商品行为记录。")
        return
    st.markdown("##### 互动最活跃的 12 个类目")
    st.caption("按浏览、收藏、加购和下单的行为总量排序；将鼠标悬停在条形上查看明细。")
    category_chart = build_category_chart(categories)
    st.plotly_chart(
        category_chart,
        use_container_width=True,
        config={"displayModeBar": False, "scrollZoom": False},
    )

    st.subheader("类目下单 TOP 10")
    order_sql = f"""
        SELECT category_id,
               COUNT(*) FILTER (WHERE behavior = 'buy') AS orders,
               COUNT(*) FILTER (WHERE behavior = 'pv') AS pv,
               COUNT(*) FILTER (WHERE behavior = 'fav') AS favorites,
               COUNT(*) FILTER (WHERE behavior = 'cart') AS carts
        FROM user_behavior WHERE {where_clause()}
        GROUP BY category_id
        HAVING COUNT(*) FILTER (WHERE behavior = 'buy') > 0
        ORDER BY orders DESC LIMIT 10
    """
    orders = query_frame(db_path, fingerprint, order_sql, params)
    if orders.empty:
        st.info("所选范围尚无下单类目。")
    else:
        orders = orders[["category_id", "orders", "pv", "favorites", "carts"]].rename(
            columns={
                "category_id": "类目 ID", "orders": "下单次数", "pv": "浏览次数",
                "favorites": "收藏次数", "carts": "加购次数",
            }
        )
        st.dataframe(orders, hide_index=True, use_container_width=True)


def show_rfm(
    db_path: str, fingerprint: tuple[int, int], params: tuple[Any, ...]
) -> None:
    st.subheader("按区间内活跃度划分用户")
    st.caption(
        "R = 距离该区间最后一条行为的天数（越小越近）；"
        "F = 行为总次数；M = 下单次数（数据集不含订单金额，故以订单次数代表 M）。"
    )
    rfm_sql, rfm_params = build_rfm_query(params)
    segments = query_frame(db_path, fingerprint, rfm_sql, rfm_params)
    if segments.empty:
        st.info("当前筛选范围没有用户行为，无法生成 RFM 分层。")
        return
    segments["segment"] = pd.Categorical(
        segments["segment"], categories=SEGMENT_ORDER, ordered=True
    )
    segments = segments.sort_values("segment")
    left, right = st.columns([1, 1.4], gap="large")
    with left:
        fig = px.pie(
            segments, names="segment", values="users", hole=0.58,
            color="segment",
            color_discrete_map={
                "高价值": COLORS["blue"], "潜力": COLORS["cyan"],
                "新": COLORS["amber"], "流失": COLORS["coral"],
            },
        )
        fig.update_traces(textposition="outside", textinfo="percent+label")
        st.plotly_chart(plotly_layout(fig, 360), use_container_width=True)
    with right:
        display = segments.rename(
            columns={
                "segment": "用户分层", "users": "用户数",
                "avg_recency_days": "平均 R（天）", "avg_frequency": "平均 F",
                "avg_orders": "平均 M（下单次数）",
            }
        )
        st.dataframe(display, hide_index=True, use_container_width=True)
        st.markdown(
            "- **高价值：** 最近活跃、频次位于较高分位且有下单。\n"
            "- **潜力：** 非高价值、非新客、非长期未活跃用户。\n"
            "- **新：** 区间内仅一次行为且尚未下单。\n"
            "- **流失：** 距区间内最后一次行为超过用户 R 的第 75 分位。"
        )
    preview_sql, preview_params = build_rfm_query(params, preview=True)
    preview = query_frame(db_path, fingerprint, preview_sql, preview_params)
    if not preview.empty:
        st.markdown("##### 用户明细预览（最多 100 位）")
        st.dataframe(preview, hide_index=True, use_container_width=True)


def show_sql(
    db_path: str,
    fingerprint: tuple[int, int],
    params: tuple[Any, ...],
    start_date: date,
    end_date: date,
    start_hour: int,
    end_hour: int,
) -> None:
    st.subheader("只读查询控制台")
    st.caption(
        f"仅允许读取 user_behavior；查询自动套用全局筛选，最多返回 {MAX_SQL_ROWS:,} 行。"
    )
    example = """SELECT behavior, COUNT(*) AS events
FROM user_behavior
GROUP BY behavior
ORDER BY events DESC"""
    user_sql = st.text_area("SQL 查询", value=example, height=150)
    if st.button("运行查询", type="primary"):
        result, error = execute_user_query(db_path, fingerprint, user_sql, params)
        if error:
            st.error(error)
        elif result is not None:
            st.success(f"查询完成：显示 {len(result):,} 行。")
            st.dataframe(result, hide_index=True, use_container_width=True)
            csv_data = result.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "下载查询结果 CSV", data=csv_data, file_name="query_result.csv",
                mime="text/csv",
            )
    st.caption(
        f"当前全局范围：{start_date} 至 {end_date}，UTC {start_hour:02d}:00–{end_hour:02d}:59。"
    )


def main() -> None:
    st.set_page_config(
        page_title="用户行为分析 · 淘宝",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    add_styles()
    st.markdown(
        """<!--
        THESIS: 用可追溯的真实行为数据串起浏览、类目与用户分层，拒绝装饰性 KPI 墙。
        OWN-WORLD: UTC 时间标尺、清晰数据墨色与蓝绿分析色，轻量图表构成工作台。
        STORY: 先确认流量和转化，再定位高潜类目与需要召回的用户。
        FIRST VIEWPORT: 标题与数据状态在上，筛选固定于侧栏，四项关键指标紧随其后。
        FORM: 操作型分析台；按趋势、商品、RFM、SQL 四条分析路径组织。
        -->""",
        unsafe_allow_html=True,
    )
    st.title("淘宝用户行为分析")
    st.markdown("从浏览轨迹读懂用户意图，让每一次运营动作都有数据依据。")

    with st.sidebar:
        st.header("分析范围")
        db_path = st.text_input("DuckDB 文件", value=str(DEFAULT_DB))
        resolved_path = str(Path(db_path).expanduser().resolve())
        if not os.path.isfile(resolved_path):
            st.warning("尚未找到分析数据库。请先运行 data_preprocess.py 导入数据。")
            st.code(
                "python data_preprocess.py --input UserBehavior.csv --db user_behavior.duckdb",
                language="bash",
            )
            return
        try:
            fingerprint = _database_fingerprint(resolved_path)
            min_time, max_time, total_rows = get_date_bounds(resolved_path, fingerprint)
        except (duckdb.Error, OSError) as exc:
            st.error(f"无法读取数据库：{exc}")
            return
        if not total_rows or min_time is None or max_time is None:
            st.warning("数据库中的 user_behavior 表没有可分析的记录。")
            return

        st.caption(f"共 {total_rows:,} 条行为记录")
        minimum = min_time.date()
        maximum = max_time.date()
        selected_dates = st.date_input(
            "日期区间", value=(minimum, maximum), min_value=minimum, max_value=maximum
        )
        if isinstance(selected_dates, tuple):
            selected_start = selected_dates[0] if selected_dates else None
            selected_end = selected_dates[1] if len(selected_dates) > 1 else None
            start_date = selected_start or selected_end or minimum
            end_date = selected_end or selected_start or minimum
        else:
            start_date = end_date = selected_dates
        start_hour, end_hour = st.slider(
            "每日小时范围（UTC）", min_value=0, max_value=23, value=(0, 23)
        )

    params = filter_parameters(start_date, end_date, start_hour, end_hour)
    try:
        metrics = metric_query(resolved_path, fingerprint, params)
    except duckdb.Error as exc:
        st.error(f"读取行为指标失败：{exc}")
        return

    cards = st.columns(4, gap="medium")
    pv = int(metrics.get("pv") or 0)
    browse_uv = int(metrics.get("browse_uv") or 0)
    buyers = int(metrics.get("buyers") or 0)
    conversion = buyers / browse_uv if browse_uv else 0.0
    cards[0].metric("浏览量 · PV", f"{pv:,}")
    cards[1].metric("浏览用户 · UV", f"{browse_uv:,}")
    cards[2].metric("下单用户", f"{buyers:,}")
    cards[3].metric("浏览-下单转化率", f"{conversion:.1%}")

    tabs = st.tabs(["数据概览", "商品分析", "用户 RFM 分层", "简易 SQL 查询"])
    try:
        with tabs[0]:
            show_overview(resolved_path, fingerprint, params, metrics)
        with tabs[1]:
            show_products(resolved_path, fingerprint, params)
        with tabs[2]:
            show_rfm(resolved_path, fingerprint, params)
        with tabs[3]:
            show_sql(
                resolved_path, fingerprint, params, start_date, end_date,
                start_hour, end_hour,
            )
    except duckdb.Error as exc:
        st.error(f"分析查询失败：{exc}")


if __name__ == "__main__":
    main()
