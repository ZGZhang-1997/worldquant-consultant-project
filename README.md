# worldquant-consultant-project

WorldQuant BRAIN 相关调研脚本与工具。

## 环境

本项目使用 [uv](https://docs.astral.sh/uv/) 管理虚拟环境与依赖，Python 版本由
`.python-version` 固定（当前为 `3.15`）。

首次准备环境：

```powershell
uv sync
```

该命令会创建 `.venv/` 并安装项目自身（可编辑模式）。

运行脚本（无需激活）：

```powershell
uv run python your_script.py
```

或手动激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

添加依赖：

```powershell
uv add requests pandas
```

## 凭证

`credentials.json` 已被 `.gitignore` 忽略，请勿提交到版本库。

## 数据获取（BRAIN API → SQLite）

子包：[`src/worldquant_consultant_project/acquisition/`](./src/worldquant_consultant_project/acquisition/)
读取 `credentials.json` 中的 `email` / `password`（也可用环境变量
`BRAIN_EMAIL` / `BRAIN_PASSWORD` 覆盖），通过 BRAIN REST API 抓取指定
`instrument / region / delay / universe` 下的**全部数据集与数据字段**，写入本地
SQLite（默认 `data/brain.db`）。

### 全量抓取

```powershell
uv run python -m worldquant_consultant_project.acquisition
```

默认口径为 `EQUITY / USA / delay=1`，会逐个探测 universe
（TOP3000、TOP2000、TOP1000、TOP500、TOP200、TOPSP500、MINVOL1M、ILLIQUID_MINVOL1M），
API 拒绝的组合记入 `universes` 表并跳过。请求严格遵循 `Retry-After` 限速，
日志写入 `data/logs/crawl-<时间戳>.log`。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--universes TOP3000,TOP1000` | 只抓指定 universe（跳过探测） |
| `--datasets fundamental6,pv1` | 只抓指定数据集（调试/冒烟） |
| `--max-fields 60` | 每个数据集最多抓 N 条（冒烟） |
| `--db data/brain_smoke.db` | 指定数据库文件 |
| `--force` | 重抓已标记 done 的数据集（默认断点续抓） |
| `--dry-run` | 只探测 API，不写库 |
| `--exclude-types VECTOR` | 跳过指定字段类型 |
| `--log-level DEBUG` | 控制台/文件日志级别 |

退出码：`0` 全部成功；`2` 鉴权失败；`3` 部分数据集失败（已抓数据保留，
重新执行即可续抓）。

### 冒烟示例

```powershell
uv run python -m worldquant_consultant_project.acquisition `
  --universes TOP3000 --datasets fundamental6 --max-fields 60 --db data/brain_smoke.db
```

### 库表结构

| 表 / 视图 | 内容 |
|---|---|
| `crawl_runs` | 每次运行的起止时间、口径、状态（running/completed/partial/failed） |
| `universes` | universe 探测结果（`is_valid`、错误信息） |
| `datasets` | 数据集元数据：名称、分类、字段数、覆盖率、`raw_json` |
| `data_fields` | 字段主表：`field_id`、`type`(MATRIX/VECTOR/GROUP)、描述、覆盖率、分类、`raw_json` |
| `crawl_state` | 每个数据集抓取进度，用于断点续抓 |
| `v_field_counts` | 按 universe/数据集汇总字段数与各类型数量 |

`data_fields` 的主键为 `(instrument_type, region, delay, universe, dataset_id, field_id)`，
重复运行以 upsert 写入，不会产生重复行。

## 字段打标签（DeepSeek / Gemini → brain.db）

子包：[`src/worldquant_consultant_project/labeling/`](./src/worldquant_consultant_project/labeling/)
同一份 [`system_prompt.txt`](./system_prompt.txt)、同一套用户提示词格式与校验/重试/续跑逻辑，
通过 `--provider` 切换后端：

| provider | 默认模型 | 凭证字段 | 写入列前缀 | 审计表 | 视图 |
|---|---|---|---|---|---|
| `deepseek`（默认） | `deepseek-flash` | `deepseek_api_key` | `deepseek_` | `deepseek_labels` | `v_deepseek_field_labels` |
| `gemini` | `gemini-3.8-flash` | `gemini_api_key` | `gemini_` | `gemini_labels` | `v_gemini_field_labels` |

凭证也可用环境变量 `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` 覆盖；两个后端均关闭思考模式并使用
JSON 结构化输出（Gemini 额外使用 `responseSchema` 约束枚举）。

### 运行

```powershell
# 先预览提示词与待处理数量（不调用 API）
uv run python -m worldquant_consultant_project.labeling --dry-run

# 冒烟：只打 20 条
uv run python -m worldquant_consultant_project.labeling --limit 20

# 全量（断点续跑，已成功的字段自动跳过）
uv run python -m worldquant_consultant_project.labeling

# 切换到 Gemini
uv run python -m worldquant_consultant_project.labeling --provider gemini
```

### 参数

| 参数 | 说明 |
|---|---|
| `--provider deepseek\|gemini` | 打标签后端，默认 `deepseek` |
| `--limit 20` | 只处理 N 个字段（冒烟） |
| `--dataset fundamental6,pv1` | 只处理指定数据集 |
| `--workers 8` | 并发线程数（默认 8） |
| `--model` | 省略时取该 provider 的默认模型 |
| `--thinking` | 启用思考模式（默认关闭，分类任务无需） |
| `--force` | 忽略已成功的记录，强制重新打标签 |
| `--dry-run` | 打印首条 user prompt 与待处理数，不调用 API |
| `--db` / `--prompt` / `--credentials` / `--log-dir` | 路径覆盖 |

退出码：`0` 全部成功；`2` 凭证或提示词缺失；`3` 部分字段失败（重新执行即可续跑）。

### 写入内容

- 按 `(dataset_id, field_id)` 去重后调用模型（4,367 次而非 26,202 次），结果一次 `UPDATE`
  回填该字段在 6 个 universe 下的所有行
- `data_fields` 末尾按 provider 追加 5 列（值均限定在 `system_prompt.txt` 的枚举内）：
  `<provider>_category`、`<provider>_math_dimension`、`<provider>_polarity`、
  `<provider>_domain_support`、`<provider>_frequency`
  （当前已有 `deepseek_*` 与 `gemini_*` 两套，可并存对比）
- 审计表 `<provider>_labels`（主键 `model + dataset_id + field_id`）记录原始响应、
  token 用量、重试次数、状态（`ok` / `invalid` / `error`）与提示词 sha256
- 视图 `v_<provider>_field_labels`：已打标签字段的扁平视图，便于下游取用

## 因子回测（Fast Expression → simulations.db）

子包：[`src/worldquant_consultant_project/simulation/`](./src/worldquant_consultant_project/simulation/)
把 Fast Expression 因子提交到 BRAIN 模拟接口，轮询完成后取 `/alphas/{id}` 指标，
写入独立数据库 `data/simulations.db`（与 `brain.db` 分开）。

### 作为函数调用

```python
from worldquant_consultant_project.simulation import SimulationSettings, run_simulation

outcome = run_simulation(
    "rank(ts_delta(close, 5))",
    settings=SimulationSettings(decay=4, universe="TOP1000"),  # 省略则用默认参数
    db_path="data/simulations.db",                              # 省略则用默认路径
)
outcome.status      # completed / failed
outcome.alpha_id
outcome.metrics["is"]["sharpe"]
```

无论成功、表达式错误还是超时，都会写入一条记录（失败记录带 `error`），不会抛未捕获异常。

### 命令行

```powershell
uv run python -m worldquant_consultant_project.simulation --expression "rank(ts_delta(close, 5))" --decay 4
```

| 参数 | 说明 |
|---|---|
| `--expression` | 必填，Fast Expression 字符串 |
| `--region` / `--universe` / `--delay` | 默认 `USA` / `TOP3000` / `1` |
| `--decay` / `--neutralization` / `--truncation` | 默认 `0` / `SUBINDUSTRY` / `0.08` |
| `--instrument-type` | 默认 `EQUITY` |
| `--db` / `--timeout` / `--credentials` / `--log-dir` | 路径与轮询超时（默认 900s） |

退出码：`0` 回测完成；`2` 凭证/鉴权失败；`3` 模拟失败（表达式错误、超时等，记录仍入库）。

### 库表结构（`data/simulations.db`）

只有**一张表** `simulations`，一行一次回测，共 120 列：

| 列组 | 内容 |
|---|---|
| 身份与参数 | `sim_uid`（主键）、`expression`、`language`、`instrument_type`、`region`、`universe`、`delay`、`decay`、`neutralization`、`truncation`、`pasteurization`、`unit_handling`、`nan_handling`、`settings_json`、`settings_hash` |
| 结果标识 | `braint_sim_id`、`alpha_id`、`status`（`completed`/`failed`）、**`has_error`**（0/1）、`error` |
| 可提交性 | **`submittable`**（1 可提交 / 0 被检查项卡住 / NULL 无检查数据）、**`submit_blockers`**（卡住的检查项 JSON） |
| 时间与追溯 | `submitted_at`、`finished_at`、`duration_seconds`、`raw_simulation_json`、`raw_alpha_json` |
| 样本指标 | `<sample>_*`，`sample ∈ is / os / train / test / prod`，每个样本 19 列（见下） |

每个样本的 19 列：

| 列 | 含义 |
|---|---|
| `<sample>_sharpe` / `_fitness` / `_returns` / `_turnover` / `_drawdown` / `_margin` / `_pnl` | 核心指标 |
| `<sample>_book_size` / `_long_count` / `_short_count` | 账簿规模与多空数量 |
| `<sample>_self_correlation` / `_prod_correlation` | 自相关 / 生产相关 |
| `<sample>_start_date` / `_drawdown_start_date` | 起始日与回撤起始日 |
| `<sample>_checks_passed` / `_checks_failed` / `_checks_pending` | 检查项通过 / 失败 / 待定计数 |
| `<sample>_checks_json` | 检查明细 JSON（`name`、`result`、`value`、`limit`、`year`、`message`） |
| `<sample>_metrics_json` | 未展开指标的兜底 JSON |

未提交的 alpha 没有样本外数据，此时 `os_*` / `train_*` / `test_*` / `prod_*` 为 `NULL`。
旧版四表结构（`simulation_results` / `simulation_metrics` / `simulation_checks`）会在
首次连接时自动迁移进单表并删除，无需手工处理。

### 关于「是否可提交」

BRAIN 的 `/alphas/{id}` 响应里**没有**「能否提交」字段，只有 `status`（`UNSUBMITTED` 等）、
`stage`、`grade` 与 `is.checks[]`。提交资格由检查项决定，因此库里用两列派生：

- `submittable = 1`：`is.checks` 全部 `PASS`
- `submittable = 0`：存在 `FAIL` 或 `PENDING`（`SELF_CORRELATION` 未算完也算卡住）
- `submittable = NULL`：没有检查数据（模拟失败）

`submit_blockers` 例：
`[{"name":"LOW_SHARPE","result":"FAIL","value":0.3,"limit":1.25},{"name":"SELF_CORRELATION","result":"PENDING","value":null,"limit":null}]`

两张历史列（`is_checks_passed/failed/pending`）保留，可直接用 SQL 复核：
`WHERE submittable = 1` 即所有达标因子。

BRAIN 的轮询约定是「响应带 `Retry-After` 头即未完成」，客户端据此等待并兼容
`{"progress": 0.x}` 的进度体。

