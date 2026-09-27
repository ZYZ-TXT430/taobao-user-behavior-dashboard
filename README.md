# 淘宝用户行为交互式分析仪表盘

[![Tests](https://github.com/ZYZ-TXT430/taobao-user-behavior-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/ZYZ-TXT430/taobao-user-behavior-dashboard/actions/workflows/tests.yml)

以阿里天池淘宝用户行为数据为基础的本地分析应用。Python 负责分块清洗，DuckDB 负责持久化与分析，Streamlit 和 Plotly 提供可交互的中文仪表盘。原始行为仅包括浏览、收藏、加购、下单；数据集不提供订单金额，因此 RFM 的 M 使用下单次数，不代表销售额。

## 技术栈

- Python 3.8–3.11（依赖锁定版本兼容范围）
- DuckDB：列式本地数据库及只读分析
- Streamlit：交互式应用
- pandas、Plotly：分析结果处理和图表
- SQLGlot：简易 SQL 查询的语法树检查

## 数据集

从 [阿里云天池淘宝用户行为数据集](https://tianchi.aliyun.com/dataset/46) 获取全量数据，并按数据集页面的使用条款下载和使用。本地开发样本可从 [PaddleRec 引用的公开 ZIP 镜像](https://paddlerec.bj.bcebos.com/tree-based/data/UserBehavior.csv.zip) 获取；仓库不分发数据文件。此前验证用的本地样本包含压缩包开头的 221,010 条完整记录，覆盖 `pv/fav/cart/buy` 四类行为，但不代表完整或随机抽样数据。需要使用时请按安装步骤准备好 CSV。原始 CSV 无表头，字段依次为用户 ID、商品 ID、商品类目 ID、行为类型、Unix 秒时间戳。

## 安装与运行

在项目目录准备好 `UserBehavior.csv` 后执行：

```bash
# Windows：选择已安装的 3.8–3.11 版本；也可将 3.8 替换为 3.9/3.10/3.11
py -3.8 -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux：python3.8 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt

python data_preprocess.py
streamlit run app.py
```

预处理默认从项目目录读取 `UserBehavior.csv`，并生成 `user_behavior.duckdb`。也可以显式指定路径与分块大小：

```bash
python data_preprocess.py --input "D:\data\UserBehavior.csv" --db "D:\data\behavior.duckdb" --chunk-size 100000
streamlit run app.py
```

运行 Streamlit 后，在侧栏填写数据库文件位置即可分析。时间戳按 UTC 解读。

## 功能

- **数据预处理：** 流式分块读取 CSV，兼容中英文表头；过滤无效 ID、非法行为和异常时间戳；去重后事务式替换 `user_behavior` 表，并报告记录、用户、商品及无效/重复行数。失败时回滚，不覆盖旧表。
- **数据概览：** PV、浏览 UV、下单用户、浏览转化率；每日 PV/UV 趋势、小时浏览分布、行为构成及筛选结果洞察。
- **商品分析：** 类目行为量排行与类目下单 TOP 10。
- **用户 RFM 分层：** 依据当前时间范围内的最近行为、行为频次和下单次数划分高价值、潜力、新、流失用户，并提供用户明细预览。
- **简易 SQL 查询：** 仅允许单条、只读的 `SELECT`/`WITH` 查询，限定数据源为 `user_behavior`，自动沿用全局日期时间筛选，拦截注释、多语句、非查询语句和外部文件读取，并限制最多显示 1,000 行。

## 界面截图

> 截图占位：运行 `streamlit run app.py` 后，可在此处补充数据概览、商品分析与 RFM 页面截图。

## 开源说明

本项目代码按 MIT License 开源，详见仓库根目录 `LICENSE`。淘宝用户行为数据由数据集提供方持有，使用、再分发和展示时须遵守天池数据集页面中的授权条款；本项目代码许可证不授予数据集的额外权利。
