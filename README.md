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

两张表（旧版宽表会在首次连接时自动丢弃重建）：

**`simulations`** —— 一行一次回测

| 列 | 含义 |
|---|---|
| `sim_uid` | 回测 id（主键） |
| `expression` | 因子表达式 |
| `tentative_submittable` | 初判可提交（1/0）：除 `check_self_correlation` 外的检查全部 `PASS` 才是 1 |
| `settings_json` | 回测设置整包 JSON（universe / delay / decay / neutralization / truncation … 不再拆列） |
| `completed` | 是否跑完（1/0） |
| `simulated_at` | 回测开始运行的时间（不是提交 alpha 的时间） |
| `duration_seconds` | 回测耗时 |
| `is_sharpe`、`is_fitness`、`is_returns`、`is_turnover`、`is_drawdown`、`is_margin`、`is_pnl`、`is_book_size`、`is_long_count`、`is_short_count`、`is_start_date` | 样本内（IS）全部指标，每项一列 |
| `check_<检查名>` | 提交检查逐项一列，取值为 `PASS` / `FAIL` / `PENDING`（如 `check_low_sharpe`、`check_self_correlation`）；**首次遇到新检查项会自动加列** |

**`simulation_details`** —— 1:1 侧表，承载不宜进主表的信息

| 列 | 含义 |
|---|---|
| `sim_uid` | 主键，外键指向 `simulations` |
| `alpha_id` / `braint_sim_id` | BRAIN 侧标识 |
| `error` | 失败原因（含表达式报错位置） |
| `finished_at` | 完成时间 |
| `other_samples_json` | `os` / `train` / `test` / `prod` 样本指标 JSON |
| `is_extra_json` | IS 中未展开为列的额外字段兜底 |
| `checks_json` | 检查明细（含 `value`、`limit`、`year`、`message`） |
| `raw_simulation_json` / `raw_alpha_json` | 原始响应，便于追溯 |

视图 `v_simulation_summary`：表达式 + 完成状态 + IS 核心指标 + alpha_id。

### 关于「是否可提交」

BRAIN 的 `/alphas/{id}` 响应里**没有**「能否提交」字段，提交资格完全由 `is.checks[]` 决定。

`tentative_submittable` 是回测后即可得到的初判：**除 `check_self_correlation` 之外的 7 个检查
全部为 `PASS` 时才记 1**，否则记 0。之所以排除自相关，是因为它要等到真正提交时才计算，
未算完会一直显示 `PENDING`，不应作为回测阶段的淘汰依据。

需要更严格的完整判定（含自相关）时，直接查检查列：

```sql
SELECT expression, alpha_id
FROM simulations s JOIN simulation_details d ON d.sim_uid = s.sim_uid
WHERE s.completed = 1
  AND s.check_low_sharpe = 'PASS'
  AND s.check_low_fitness = 'PASS'
  AND s.check_self_correlation = 'PASS';
```

`PENDING`（如 `SELF_CORRELATION` 尚未算完）同样不算通过。

BRAIN 的轮询约定是「响应带 `Retry-After` 头即未完成」，客户端据此等待并兼容
`{"progress": 0.x}` 的进度体。

## 模板生成因子（Template.json → alpha.db）

子包：[`src/worldquant_consultant_project/Template_based_alpha_generate/`](./src/worldquant_consultant_project/Template_based_alpha_generate/)
读取 [`Template.json`](./src/worldquant_consultant_project/Template_based_alpha_generate/Template.json)，
按每个槽位的标签规则从 `brain.db` 筛出字段，填入 `formula_pattern` 生成 Fast Expression；
多槽位模板做**笛卡尔积**，结果写入 `data/alpha.db`。

### 运行

```powershell
# 只看候选数与组合数，不写库
uv run python -m worldquant_consultant_project.Template_based_alpha_generate --dry-run

# 小批量试跑（每模板 5 条，写测试库）
uv run python -m worldquant_consultant_project.Template_based_alpha_generate `
  --db data/alpha_test.db --limit-per-template 5

# 全量生成
uv run python -m worldquant_consultant_project.Template_based_alpha_generate
```

| 参数 | 说明 |
|---|---|
| `--templates` | 模板文件，默认模块内 `Template.json` |
| `--label-source` | `union`（默认，所有模型标签取并集）/ `intersection` / `deepseek` / `gemini` |
| `--template T01_Relative_Valuation,...` | 只生成指定模板 |
| `--limit-per-template N` | 每模板最多 N 条（调试） |
| `--db` / `--brain-db` | 输出库（默认 `data/alpha.db`）/ 字段库（默认 `data/brain.db`） |
| `--dry-run` / `--clear` | 只统计 / 生成前清空 |

退出码：`0` 成功；`2` 模板或库路径问题；`3` 槽位无候选字段。

### 标签来源（并集，且自动适配新模型）

`--label-source union` 会遍历 [`labeling/config.py`](./src/worldquant_consultant_project/labeling/config.py)
中注册、且在 `data_fields` 中确有标签列的所有 provider（当前 `deepseek` + `gemini`），
**各自求槽位字段集后取并集**。以后新增模型只需在该注册表登记并跑完打标签，
生成器会自动纳入，无需改代码。

实测组合数：`union` 29,680；`deepseek` 8,753；`intersection` 7,496。

### 库表结构（`data/alpha.db`）

| 表 / 视图 | 内容 |
|---|---|
| `alphas` | 一行一个因子：`alpha_uid`、`template_id`、`template_name`、`expression`、`expression_hash`（唯一，去重）、`slot_count`、`formula_pattern`、`label_source`、`slot_fill_json`、`run_id`、`generated_at` |
| `alpha_slots` | 每因子每槽位一行：`slot_name`、`slot_index`、`dataset_id`、`field_id`、`description`、`field_type`、`matched_providers`（命中规则的模型）、`labels_json`（各模型 5 维标签） |
| `v_alpha_expressions` | 表达式速查视图 |

同一表达式重复生成会被 `expression_hash` 唯一索引拦下，重跑不产生重复行。

### 模板定义格式

```json
{
  "template_id": "T04_Analyst_Revision_Drift",
  "slot_count": 1,
  "formula_pattern": "group_rank(ts_delta({Slot_Expectation}, 20), subindustry)",
  "slots": {
    "Slot_Expectation": {
      "category": ["analyst"],
      "math_dimension": ["price_level", "ratio_relative", "score_bounded"],
      "polarity": ["positive"],
      "domain_support": ["real_unbounded", "strictly_positive", "bounded_interval"],
      "frequency": ["daily_high", "event_irregular"]
    }
  }
}
```

- 同一模板内同一槽位可在公式中**重复出现**（如 `ts_delay({Slot_Target}, 252)`），按 `slot_count` 计为 1 个槽位
- 校验会拦截：占位符与 `slots` 不匹配、`slot_count` 与实际占位符数不符、维度名或枚举值非法

