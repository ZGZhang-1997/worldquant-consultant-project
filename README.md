# worldquant-consultant-project

WorldQuant BRAIN 相关脚本与工具。

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

| 参数                            | 说明                                     |
| ------------------------------- | ---------------------------------------- |
| `--universes TOP3000,TOP1000` | 只抓指定 universe（跳过探测）            |
| `--datasets fundamental6,pv1` | 只抓指定数据集（调试/冒烟）              |
| `--max-fields 60`             | 每个数据集最多抓 N 条（冒烟）            |
| `--db data/brain_smoke.db`    | 指定数据库文件                           |
| `--force`                     | 重抓已标记 done 的数据集（默认断点续抓） |
| `--dry-run`                   | 只探测 API，不写库                       |
| `--exclude-types VECTOR`      | 跳过指定字段类型                         |
| `--log-level DEBUG`           | 控制台/文件日志级别                      |

退出码：`0` 全部成功；`2` 鉴权失败；`3` 部分数据集失败（已抓数据保留，
重新执行即可续抓）。

### 冒烟示例

```powershell
uv run python -m worldquant_consultant_project.acquisition `
  --universes TOP3000 --datasets fundamental6 --max-fields 60 --db data/brain_smoke.db
```

### 库表结构

| 表 / 视图          | 内容                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------- |
| `crawl_runs`     | 每次运行的起止时间、口径、状态（running/completed/partial/failed）                      |
| `universes`      | universe 探测结果（`is_valid`、错误信息）                                             |
| `datasets`       | 数据集元数据：名称、分类、字段数、覆盖率、`raw_json`                                  |
| `data_fields`    | 字段主表：`field_id`、`type`(MATRIX/VECTOR/GROUP)、描述、覆盖率、分类、`raw_json` |
| `crawl_state`    | 每个数据集抓取进度，用于断点续抓                                                        |
| `v_field_counts` | 按 universe/数据集汇总字段数与各类型数量                                                |

`data_fields` 的主键为 `(instrument_type, region, delay, universe, dataset_id, field_id)`，
重复运行以 upsert 写入，不会产生重复行。

### 已提交因子镜像（平台 ACTIVE → `alpha.db`）

子模块：[`acquisition/active_alphas.py`](./src/worldquant_consultant_project/acquisition/active_alphas.py)
读取平台 `GET /users/self/alphas?status=ACTIVE`（已提交的因子），写入 `alpha.db` 的
`active_alphas` 表，并**按表达式挂到 `alphas`**：本地没有这个因子就自动登记一条。

```powershell
uv run python -m worldquant_consultant_project.acquisition.active_alphas            # 同步一次
uv run python -m worldquant_consultant_project.acquisition.active_alphas --dry-run  # 只看平台上有多少
```

- **自动执行**：`simulation.batch` 与 `simulation.optimize` 每次启动都会先跑一次同步
  （`--skip-active-sync` 可跳过）；同步失败只记 warning，不影响后面的批量任务
- **平台状态值是 `ACTIVE`**（不是 `SUBMITTED`）；列表接口最多只能翻到前 1,000 条，
  超出时会截断并在日志里提示
- **作用**：已提交的因子会被 `BACKTESTED_SQL` 视为「已有判定」，批量回测不会再跑它

| 列 | 内容 |
|---|---|
| `braint_alpha_id` | 主键，BRAIN 的 alpha id |
| `alpha_uid` | 外键 → `alphas.alpha_uid`（级联删除） |
| `expression` | Fast Expression（来自 `regular.code`） |
| `status` / `grade` | 平台状态（`ACTIVE`）/ 评级（`AVERAGE` 等） |
| `submitted_at` / `created_at` | `dateSubmitted` / `dateCreated` |
| `settings_json` | 回测参数整包（region/universe/delay/decay/neutralization/truncation…） |
| `is_*` | 样本内指标（`is_sharpe`、`is_fitness`、`is_turnover`… 与回测表同名列） |
| `self_correlation` / `prod_correlation` | 自相关 / 生产相关（仅已提交因子有值） |
| `checks_json` | 逐项提交检查结果 |
| `os_json` / `train_json` / `test_json` | 其它样本指标 |
| `raw_alpha_json` / `synced_at` | 原始负载 / 本次同步时间 |

```sql
-- 已提交的因子，按评级与夏普看
SELECT aa.braint_alpha_id, aa.grade, aa.submitted_at, aa.is_sharpe,
       json_extract(aa.settings_json, '$.decay') AS decay, aa.expression
FROM active_alphas AS aa
ORDER BY aa.submitted_at DESC;
```

## 字段打标签（DeepSeek / Gemini → brain.db）

子包：[`src/worldquant_consultant_project/labeling/`](./src/worldquant_consultant_project/labeling/)
同一份 [`system_prompt.txt`](./system_prompt.txt)、同一套用户提示词格式与校验/重试/续跑逻辑，
通过 `--provider` 切换后端：

| provider             | 默认模型             | 凭证字段             | 写入列前缀    | 审计表              | 视图                        |
| -------------------- | -------------------- | -------------------- | ------------- | ------------------- | --------------------------- |
| `deepseek`（默认） | `deepseek-flash`   | `deepseek_api_key` | `deepseek_` | `deepseek_labels` | `v_deepseek_field_labels` |
| `gemini`           | `gemini-3.8-flash` | `gemini_api_key`   | `gemini_`   | `gemini_labels`   | `v_gemini_field_labels`   |

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

| 参数                                                        | 说明                                        |
| ----------------------------------------------------------- | ------------------------------------------- |
| `--provider deepseek\|gemini`                              | 打标签后端，默认 `deepseek`               |
| `--limit 20`                                              | 只处理 N 个字段（冒烟）                     |
| `--dataset fundamental6,pv1`                              | 只处理指定数据集                            |
| `--workers 8`                                             | 并发线程数（默认 8）                        |
| `--model`                                                 | 省略时取该 provider 的默认模型              |
| `--thinking`                                              | 启用思考模式（默认关闭，分类任务无需）      |
| `--force`                                                 | 忽略已成功的记录，强制重新打标签            |
| `--dry-run`                                               | 打印首条 user prompt 与待处理数，不调用 API |
| `--db` / `--prompt` / `--credentials` / `--log-dir` | 路径覆盖                                    |

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

## 因子回测（Fast Expression → alpha.db）

子包：[`src/worldquant_consultant_project/simulation/`](./src/worldquant_consultant_project/simulation/)
把 Fast Expression 因子提交到 BRAIN 模拟接口，轮询完成后取 `/alphas/{id}` 指标，
回测结果**按 `alpha_uid` 直接挂到 `data/alpha.db`**（与因子同库），一个因子可以有多条不同参数的
回测记录。（原 `data/simulations.db` 已并入 `alpha.db` 并删除。）

### 作为函数调用

```python
from worldquant_consultant_project.simulation import SimulationSettings, run_simulation

outcome = run_simulation(
    "rank(ts_delta(close, 5))",
    settings=SimulationSettings(decay=4, universe="TOP1000"),  # 省略则用默认参数
    db_path="data/alpha.db",                                    # 省略则用默认路径
)
outcome.status      # completed / failed
outcome.alpha_uid   # 结果挂在哪个因子上（未知表达式会自动登记进 alphas）
outcome.alpha_id    # BRAIN 侧的 alpha id（库里列名 braint_alpha_id）
outcome.metrics["is"]["sharpe"]
```

无论成功、表达式错误还是超时，都会写入一条记录（失败记录带 `error`），不会抛未捕获异常。
查某个因子的全部回测：

```python
from worldquant_consultant_project.simulation import storage

conn = storage.connect("data/alpha.db")
try:
    for row in storage.runs_for_alpha(conn, "4219b06d86ad4a84bbf2eae9b746da38"):
        print(row["simulated_at"], row["is_sharpe"], row["braint_alpha_id"])
finally:
    conn.close()
```

### 命令行

```powershell
uv run python -m worldquant_consultant_project.simulation --expression "rank(ts_delta(close, 5))" --decay 4
```

| 参数                                                         | 说明                                    |
| ------------------------------------------------------------ | --------------------------------------- |
| `--expression`                                             | 必填，Fast Expression 字符串            |
| `--region` / `--universe` / `--delay`                  | 默认 `USA` / `TOP3000` / `1`      |
| `--decay` / `--neutralization` / `--truncation`        | 默认 `0` / `SUBINDUSTRY` / `0.08` |
| `--instrument-type`                                        | 默认 `EQUITY`                         |
| `--db` / `--timeout` / `--credentials` / `--log-dir` | 路径与轮询超时（默认 900s）             |

退出码：`0` 回测完成；`2` 凭证/鉴权失败；`3` 模拟失败（表达式错误、超时等，记录仍入库）。

### 批量回测（alpha.db 未回测因子 → 多线程并发）

```powershell
uv run python -m worldquant_consultant_project.simulation.batch --dry-run   # 先看待回测数量
uv run python -m worldquant_consultant_project.simulation.batch             # 全量并发回测
uv run python -m worldquant_consultant_project.simulation.batch --limit 20  # 只跑 20 个
```

- **取数**：从 `data/alpha.db` 的 `alphas` 表读取**还没被平台判定过**的因子（按 `alpha_uid` 排序）——「判定过」指有一条回测出了结果，或有一条回测被判为表达式本身有错
- **并发**：线程池大小取自 [`maximum_concurrent_number`](./src/worldquant_consultant_project/simulation/maximum_concurrent_number)（当前 `3`，即账号允许的同时回测数），`--workers` 可临时覆盖
- **参数**：取自 [`batch_simulation_settings.json`](./src/worldquant_consultant_project/simulation/batch_simulation_settings.json)（`type` + `settings`），每个线程独立登录并复用会话
- **写回**：回测完成（无论成功或因子报错）后自动刷新该因子的汇总列（`number_of_simulations`、`tentative_submittable`、`is_formatting_error`、`max_*`）；本地崩溃（未真正提交）与平台报错不留下记录，因子下次仍会被取到
- **覆盖**：`alpha_simulations` 上建有 `(expression, settings_json)` 唯一索引，同一表达式 + 同一参数重跑会**覆盖原行并保留原 `sim_uid`**，不会新增重复记录；换个参数则是**新增一行**，同一个 `alpha_uid` 下可以有多条
- **限流**：提交遇到 429（超出并发额度）会按 `Retry-After` 等待后重试，不会让该因子失败
- **优化设置**：[`optimization_simulation_settings.json`](./src/worldquant_consultant_project/simulation/optimization_simulation_settings.json) 由下面的「参数优化」使用

| 参数 | 说明 |
|---|---|
| `--alpha-db` | 默认 `data/alpha.db`（因子与回测都在这里） |
| `--db` | 已废弃的别名，等价于 `--alpha-db` |
| `--settings` / `--concurrency-file` | 覆盖默认配置文件路径 |
| `--workers N` | 覆盖并发数（默认读配置文件） |
| `--limit N` | 只回测 N 个 |
| `--timeout` / `--progress-every` | 单条轮询超时（默认 900s）/ 进度日志间隔 |
| `--dry-run` | 只统计待回测数量 |
| `--reconcile-only` | 只收尾「已提交但未记录结果」的行，然后退出 |
| `--no-reconcile` | 跳过启动时的自动收尾 |
| `--stale-minutes` | 只回进度（无 `Retry-After`）时，超过多少分钟判为停滞（默认 30） |
| `--abandon-after-hours` | 平台一直回 `Retry-After` 时，超过多少小时放弃（默认 6） |
| `--purge-platform-errors` | 删除「平台原因」导致失败的记录，使对应因子重新可回测 |
| `--skip-active-sync` | 启动时不镜像平台的 ACTIVE 因子（见「已提交因子镜像」） |

退出码：`0` 全部成功；`2` 配置/鉴权失败；`3` 存在失败因子（失败记录已入库，可据此排查）。

### 参数优化（挑出好因子 → 参数网格重跑）

```powershell
# 只看会选中多少因子、还有多少组参数要跑，不提交
uv run python -m worldquant_consultant_project.simulation.optimize --delay delay1 --dry-run
# 实际执行（delay1 是默认值，可省略）
uv run python -m worldquant_consultant_project.simulation.optimize
# 换成 delay=0 的那一组阈值
uv run python -m worldquant_consultant_project.simulation.optimize --delay delay0
# 两组连着跑（先 delay1 再 delay0）
uv run python -m worldquant_consultant_project.simulation.optimize --delay both
```

两步：

1. **挑选因子**：`--delay` 决定用哪一组阈值，从
   [`settings_for_choosing_alphas_to_optimize.json`](./src/worldquant_consultant_project/simulation/settings_for_choosing_alphas_to_optimize.json)
   读 `delay1` / `delay0` 的 `is_sharpe_lower_limit` 与 `is_fitness_lower_limit`，
   要求 `alphas.max_is_sharpe_delay{1,0}` 与 `max_is_fitness_delay{1,0}` **同时**大于等于阈值；
   `--delay both` 会把两组阈值各跑一遍，日志与汇总按 delay 分组输出
2. **参数扫描**：把
   [`optimization_simulation_settings.json`](./src/worldquant_consultant_project/simulation/optimization_simulation_settings.json)
   的 `search_space` 各轴做**笛卡尔积**，逐组参数回测这些因子；`delay` 一律用启动参数，
   文件里若写了 `delay` 会被忽略（并打一条 warning）

- **跳过已有判定的组合**：每组回测提交前按 `(expression, settings_json)` 查库，**已有结果或已有
  「因子原因」报错**的记录才跳过；只有一条待定占位行（轮询超时、还没有结果）的组合会重新提交。
  中途停掉再跑一次会自动续上，不会重复烧额度
- **写回**：与批量回测共用同一条落库路径，`alphas` 上的 `number_of_simulations`、`max_*`、
  `tentative_submittable` 会随扫描结果一起更新（新参数的回测成为该因子的新记录）
- **并发**：线程池大小同样取自 `maximum_concurrent_number`（`--workers` 可覆盖）
- **额度保护**：连续平台失败达到 `max(5, 2×workers)` 次会中止整轮，剩余组合保持未跑，下次续跑；
  `--delay both` 时一组被中止就不再开始下一组

| 参数 | 说明 |
|---|---|
| `--delay` | `delay1`（默认）/ `delay0` / `both`（先 delay1 后 delay0），也接受 `1` / `0` |
| `--selection` | 选因子的阈值文件，默认 `settings_for_choosing_alphas_to_optimize.json` |
| `--search-space` | 参数网格文件，默认 `optimization_simulation_settings.json` |
| `--alpha-db` / `--concurrency-file` | 与批量回测相同 |
| `--workers N` / `--limit N` | 覆盖并发数 / 只优化 N 个因子 |
| `--timeout` / `--progress-every` | 单条轮询超时 / 进度日志间隔 |
| `--dry-run` | 只报告选中的因子数与剩余待跑组合数 |
| `--skip-active-sync` | 启动时不镜像平台的 ACTIVE 因子 |

退出码：`0` 全部成功（或无事可做）；`2` 配置/鉴权失败；`3` 存在失败因子。

### 错误分类：平台原因 vs 因子原因

只有**平台真正评估过该因子**并拒绝它，才算「有判定」；平台侧问题（额度、会话、服务异常）
不算，因子会留在待回测队列里下次重试。

| 类型 | 典型情况 | 是否入库 | 是否算「已被平台判定」 |
|---|---|---|---|
| **成功** | 回测完成 | 是（含 IS 指标） | 是（计入 `number_of_simulations`） |
| **因子原因** | 表达式语法错误、`Operator ts_delta does not support event inputs`、未知字段（HTTP 400 / 模拟 `status=ERROR`） | 是（`error_kind='factor'`） | **是**（语法类还会置 `is_formatting_error=1`） |
| **平台原因** | 每日/并发额度（`DAILY_SIMULATION_LIMIT_EXCEEDED`、`CONCURRENT_SIMULATION_LIMIT_EXCEEDED`）、会话过期 401、403、5xx、连接中断 | **否**（占位行会被删除） | **否** |
| 回测超时 | 平台仍在计算 | 是（占位行，待收尾） | 否 |

配套行为：

- **401 自动重鉴权**：提交时会话过期会被识别、重新登录并重试（最多 3 次），不再被误判为因子失败
- **额度保护**：连续平台失败达到 `max(5, 2×workers)` 次，或收到每日额度错误时，批量立即停止，
  剩余因子保持待回测
- **历史数据清理**：`--purge-platform-errors` 会删除 `error_kind='platform'` 的记录，并刷新受影响因子的
  汇总列，使其重新进入待回测队列（旧记录在首次连接时会自动回填 `error_kind`）

### 平台侧停滞 / 超时的处理

**回测中轮询超时不会丢结果**：单条轮询超过 `--timeout`（默认 900s）时，不再记成失败，而是把该行
留作「占位行」（`completed=0`、无 `error`、`finished_at` 为空、保留 `braint_sim_id`），它不计入
`number_of_simulations`，等下一次收尾时再去平台取结果。

**收尾时的三种时限**（批量启动或 `--reconcile-only` 时执行）：

| 平台状态 | 处理 |
|---|---|
| 已完成 | 拉取 alpha 指标补写结果，`completed=1` |
| 报错 | 写入平台错误信息，`completed=0` + `error` |
| 平台已无此 id（404） | `error="interrupted: simulation no longer exists on the platform"` |
| 仍在计算（响应带 `Retry-After`），行龄 < `--abandon-after-hours` | 保留，下次再收尾 |
| 仍在计算但行龄 ≥ `--abandon-after-hours` | 放弃：`error="interrupted: platform still computing after Xh (...)"` |
| 只回进度且无 `Retry-After`，行龄 ≥ `--stale-minutes` | 判定停滞：`error="interrupted: stalled after Xh (...)"` |
| 只回进度且行龄 < `--stale-minutes` | 保留，下次再收尾 |

收尾成功的因子会因为那条回测变成「已完成」而计入 `number_of_simulations`，批量不会再取到它；
被判 interrupted 的行没有结果，因子仍会进入下一批。

### 库表结构（`data/alpha.db` 里的回测表）

两张表挂在外键 `alpha_uid` 上（`alphas` 删除时级联删除），一行回测 = 主表一行 + 详情表一行：

**`alpha_simulations`** —— 一行一次回测

| 列 | 含义 |
|---|---|
| `sim_uid` | 回测 id（主键） |
| `alpha_uid` | 外键 → `alphas.alpha_uid`，这是「按因子查回测」的入口 |
| `expression` | 因子表达式（冗余存一份，便于直接查看/去重） |
| `tentative_submittable` | 初判可提交（1/0）：其他检查全部 `PASS`，且 `SELF_CORRELATION` 为 `PASS` 或 `PENDING`（只有它为 `FAIL` 才算不通过） |
| `settings_json` | 回测设置整包 JSON（universe / delay / decay / neutralization / truncation … 不再拆列） |
| `completed` | 是否跑完（1/0） |
| `simulated_at` | 回测开始运行的时间（不是提交 alpha 的时间） |
| `duration_seconds` | 回测耗时 |
| `is_sharpe`、`is_fitness`、`is_returns`、`is_turnover`、`is_drawdown`、`is_margin`、`is_pnl`、`is_book_size`、`is_long_count`、`is_short_count`、`is_start_date` | 样本内（IS）全部指标，每项一列 |
| `check_<检查名>` | 提交检查逐项一列，取值为 `PASS` / `FAIL` / `PENDING`（如 `check_low_sharpe`、`check_self_correlation`）；**首次遇到新检查项会自动加列** |

同一 `(expression, settings_json)` 只保留一行：重跑会覆盖结果（保留原 `sim_uid`），且是**整行替换**——
新结果里没有的 `is_*` / `check_*` 会被清空，不会残留上一次的判定。换个参数则是新增一行，
同一 `alpha_uid` 下可以有多条。

**`alpha_simulation_details`** —— 1:1 侧表，承载不宜进主表的信息

| 列 | 含义 |
|---|---|
| `sim_uid` | 主键，外键指向 `alpha_simulations` |
| `alpha_uid` | 冗余外键 → `alphas.alpha_uid` |
| `braint_alpha_id` / `braint_sim_id` | BRAIN 侧标识（`alpha_id` 已改名为 `braint_alpha_id`，避免和 `alpha_uid` 混淆） |
| `error` | 失败原因（含表达式报错位置） |
| `error_kind` | `factor`（因子被评估后拒绝）/ `platform`（平台原因，因子未被评估） |
| `finished_at` | 完成时间 |
| `other_samples_json` | `os` / `train` / `test` / `prod` 样本指标 JSON |
| `is_extra_json` | IS 中未展开为列的额外字段兜底 |
| `checks_json` | 检查明细（含 `value`、`limit`、`year`、`message`） |
| `raw_simulation_json` / `raw_alpha_json` | 原始响应，便于追溯 |

视图 `v_alpha_simulation_summary`：`sim_uid` + `alpha_uid` + 表达式 + 完成状态 + IS 核心指标 + `braint_alpha_id`。

索引：`idx_alpha_simulations_alpha`（按因子查）、`idx_alpha_simulations_simulated`（按时间查）、
`uq_alpha_simulations_expression_settings`（同参数覆盖）、`idx_alpha_simulation_details_braint`。

一个因子可以有多次不同参数的回测：

```sql
SELECT expression FROM alphas WHERE alpha_uid = '4219b06d86ad4a84bbf2eae9b746da38';
-- 该因子的全部回测，最新在前（含每项 check）
SELECT s.simulated_at, s.settings_json, s.is_sharpe, s.check_self_correlation, d.braint_alpha_id
FROM alpha_simulations AS s
JOIN alpha_simulation_details AS d USING (sim_uid)
WHERE s.alpha_uid = '4219b06d86ad4a84bbf2eae9b746da38'
ORDER BY s.simulated_at DESC;
```

### 关于「是否可提交」

BRAIN 的 `/alphas/{id}` 响应里**没有**「能否提交」字段，提交资格完全由 `is.checks[]` 决定。

`tentative_submittable` 是回测后即可得到的初判：

- 其余 7 项检查必须全部为 `PASS`
- `SELF_CORRELATION` 为 `PASS` **或 `PENDING`** 都可以接受（提交前通常一直是 `PENDING`）
- 只有 `SELF_CORRELATION` 已经明确为 `FAIL` 时，该列才记 0

即：`其他检查全 PASS 且 SELF_CORRELATION != FAIL` → 1，否则 0。失败的回测（没有任何检查数据）记 0。

需要更严格的完整判定（含自相关）时，直接查检查列：

```sql
SELECT s.alpha_uid, s.expression, d.braint_alpha_id
FROM alpha_simulations AS s
JOIN alpha_simulation_details AS d USING (sim_uid)
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

| 参数                                      | 说明                                                                                |
| ----------------------------------------- | ----------------------------------------------------------------------------------- |
| `--templates`                           | 模板文件，默认模块内 `Template.json`                                              |
| `--label-source`                        | `union`（默认，所有模型标签取并集）/ `intersection` / `deepseek` / `gemini` |
| `--template T01_Relative_Valuation,...` | 只生成指定模板                                                                      |
| `--limit-per-template N`                | 每模板最多 N 条（调试）                                                             |
| `--db` / `--brain-db`                 | 输出库（默认 `data/alpha.db`）/ 字段库（默认 `data/brain.db`）                  |
| `--dry-run` / `--clear`               | 只统计 / 生成前清空                                                                 |

退出码：`0` 成功；`2` 模板或库路径问题；`3` 槽位无候选字段。

### 标签来源（并集，且自动适配新模型）

`--label-source union` 会遍历 [`labeling/config.py`](./src/worldquant_consultant_project/labeling/config.py)
中注册、且在 `data_fields` 中确有标签列的所有 provider（当前 `deepseek` + `gemini`），
**各自求槽位字段集后取并集**。以后新增模型只需在该注册表登记并跑完打标签，
生成器会自动纳入，无需改代码。

实测组合数（含 `type` 筛选）：6 个模板共 **7,672** 条（T01 522 / T02 1,965 / T03 4,725 / T04 183 / T05 76 / T06 201）。

### 库表结构（`data/alpha.db` 里的因子表）

| 表 / 视图               | 内容                                                                                                                                                                                                                        |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `alphas`              | **精简主表**，一行一个因子：`alpha_uid`（主键）、`expression`，外加由回测汇总出来的列：`number_of_simulations`（有结果的回测条数）、`tentative_submittable`（任一回测可提交即 1，否则 0）、`is_formatting_error`（出现过格式类报错即 1）、`max_is_sharpe`、`max_is_fitness` 以及按 delay 分组的 `max_is_sharpe_delay1` / `max_is_fitness_delay1` / `max_is_sharpe_delay0` / `max_is_fitness_delay0` |
| `alpha_details`       | 1:1 侧表：`template_id`、`template_name`、`expression_hash`（唯一，去重）、`slot_count`、`formula_pattern`、`label_source`、`slot_fill_json`、`run_id`、`generated_at` |
| `alpha_slots`         | 每因子每槽位一行：`slot_name`、`slot_index`、`dataset_id`、`field_id`、`description`、`field_type`、`matched_providers`（命中规则的模型）、`labels_json`（各模型 5 维标签）                                 |
| `alpha_simulations` + `alpha_simulation_details` | 回测记录，`alpha_uid` 外键挂回 `alphas`，一个因子可有多条（详见上文「库表结构（回测表）」） |
| `active_alphas` | 平台上已提交（ACTIVE）的因子镜像，`alpha_uid` 挂回 `alphas`（详见「已提交因子镜像」一节） |
| `v_alpha_expressions` | 表达式 + 模板 + 回测标记速查视图 |

`number_of_simulations` 等汇总列由回测自动维护：新生成的因子为 0，等它被回测后由写库逻辑更新。
查还没有被平台判定过的因子：

```sql
SELECT alpha_uid, expression FROM alphas
WHERE NOT EXISTS (
    SELECT 1 FROM alpha_simulations AS s
    JOIN alpha_simulation_details AS d ON d.sim_uid = s.sim_uid
    WHERE s.alpha_uid = alphas.alpha_uid
      AND (s.completed = 1 OR d.error_kind = 'factor')
);
```

只想回测模板生成、且还没跑过的因子时：

```sql
SELECT a.alpha_uid, a.expression
FROM alphas AS a JOIN alpha_details AS d USING (alpha_uid)
WHERE NOT EXISTS (
    SELECT 1 FROM alpha_simulations AS s
    JOIN alpha_simulation_details AS d2 ON d2.sim_uid = s.sim_uid
    WHERE s.alpha_uid = a.alpha_uid
      AND (s.completed = 1 OR d2.error_kind = 'factor')
);
```

按因子查它的全部回测：

```sql
SELECT s.simulated_at, s.is_sharpe, d.braint_alpha_id
FROM alpha_simulations AS s JOIN alpha_simulation_details AS d USING (sim_uid)
WHERE s.alpha_uid = '...' ORDER BY s.simulated_at DESC;
```

### 因子级汇总列（`alphas` 上）

每次往库里写回测（含覆盖、删除），都会用 `refresh_alpha_metrics()` 重算这几列：

| 列 | 规则 |
|---|---|
| `number_of_simulations` | 该因子**有结果**的回测条数（`completed = 1`；占位行、报错行不算） |
| `tentative_submittable` | 该因子**任一**回测的 `tentative_submittable = 1` 就记 1，否则 0（没有任何回测也是 0） |
| `is_formatting_error` | 该因子**曾经**出现过格式类报错就记 1，从未出现记 0（粘性：置 1 后不因重跑或删记录而回落） |
| `max_is_sharpe` | 该因子所有回测 `is_sharpe` 的最大值（无值为 NULL） |
| `max_is_fitness` | 该因子所有回测 `is_fitness` 的最大值（无值为 NULL） |
| `max_is_sharpe_delay1` / `max_is_fitness_delay1` | 同上，但只在 `delay = 1` 的回测里取最大 |
| `max_is_sharpe_delay0` / `max_is_fitness_delay0` | 同上，但只在 `delay = 0` 的回测里取最大 |

- delay 取自每条回测 `settings_json` 里的 `delay`（JSON 损坏或缺失时该条不参与分组统计）
- 各最大值**可以来自不同的回测**，不要求出现在同一行；某个 delay 没有回测时，对应两列为 NULL
- 「格式类报错」指表达式写法/语法层面的错误，判定词表是 `storage.FORMATTING_ERROR_MARKERS`
  （`syntax error`、`unexpected token`、`unbalanced`、`missing closing` 等）；
  像 `Operator ts_delta does not support event inputs` 这种字段/类型错误不算
- 想让某个因子的粘性标记回落，手动 `UPDATE alphas SET is_formatting_error = 0 WHERE alpha_uid = '...'` 即可

直接筛「值得提交」的因子：

```sql
SELECT alpha_uid, expression, max_is_sharpe, max_is_fitness,
       max_is_sharpe_delay1, max_is_fitness_delay1
FROM alphas
WHERE tentative_submittable = 1
ORDER BY max_is_sharpe_delay1 DESC;
```

同一表达式重复生成会被 `alpha_details.expression_hash` 唯一索引拦下，重跑不产生重复行。
旧版宽表（模板列都塞在 `alphas` 里）会在首次连接时自动拆分为上述结构，数据保留。

### 模板定义格式

```json
{
  "template_id": "T04_Analyst_Revision_Drift",
  "slot_count": 1,
  "formula_pattern": "group_rank(ts_delta({Slot_Expectation}, 20), subindustry)",
  "slots": {
    "Slot_Expectation": {
      "type": ["MATRIX"],
      "category": ["analyst"],
      "math_dimension": ["price_level", "ratio_relative", "score_bounded"],
      "polarity": ["positive"],
      "domain_support": ["real_unbounded", "strictly_positive", "bounded_interval"],
      "frequency": ["daily_high", "event_irregular"]
    }
  }
}
```

- `type` 是**字段数据类型**筛选（不是模型标签），取值为 `MATRIX` / `VECTOR` / `GROUP` / `UNIVERSE` / `SYMBOL`，
  小写会自动归一化为大写，非法值直接报错；它对应 `data_fields.type` 列，不参与 `<provider>_` 前缀
- 同一模板内同一槽位可在公式中**重复出现**（如 `ts_delay({Slot_Target}, 252)`），按 `slot_count` 计为 1 个槽位
- 校验会拦截：占位符与 `slots` 不匹配、`slot_count` 与实际占位符数不符、维度名或枚举值非法、`type` 非法
- 现有 6 个模板的分工：T01–T05 使用 `MATRIX` 字段；T06 专门使用 `VECTOR` 字段并通过 `vec_avg` 降维
