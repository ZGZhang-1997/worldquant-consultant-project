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

## 字段打标签（DeepSeek → brain.db）

子包：[`src/worldquant_consultant_project/labeling/`](./src/worldquant_consultant_project/labeling/)
读取 `credentials.json` 的 `deepseek_api_key`（也可用环境变量 `DEEPSEEK_API_KEY` 覆盖），
系统提示词来自 [`system_prompt.txt`](./system_prompt.txt)，模型默认 `deepseek-flash`
（关闭思考模式 + JSON 输出）。

### 运行

```powershell
# 先预览提示词与待处理数量（不调用 API）
uv run python -m worldquant_consultant_project.labeling --dry-run

# 冒烟：只打 20 条
uv run python -m worldquant_consultant_project.labeling --limit 20

# 全量（断点续跑，已成功的字段自动跳过）
uv run python -m worldquant_consultant_project.labeling
```

### 参数

| 参数 | 说明 |
|---|---|
| `--limit 20` | 只处理 N 个字段（冒烟） |
| `--dataset fundamental6,pv1` | 只处理指定数据集 |
| `--workers 8` | 并发线程数（默认 8） |
| `--model deepseek-flash` | 模型名，换模型时标签写入独立的列前缀需另行扩展 |
| `--thinking` | 启用思考模式（默认关闭，分类任务无需） |
| `--force` | 忽略已成功的记录，强制重新打标签 |
| `--dry-run` | 打印首条 user prompt 与待处理数，不调用 API |
| `--db` / `--prompt` / `--credentials` / `--log-dir` | 路径覆盖 |

退出码：`0` 全部成功；`2` 凭证或提示词缺失；`3` 部分字段失败（重新执行即可续跑）。

### 写入内容

- 按 `(dataset_id, field_id)` 去重后调用模型（4,367 次而非 26,202 次），结果一次 `UPDATE`
  回填该字段在 6 个 universe 下的所有行
- `data_fields` 末尾新增 5 列（值均限定在 `system_prompt.txt` 的枚举内）：
  `deepseek_category`、`deepseek_math_dimension`、`deepseek_polarity`、
  `deepseek_domain_support`、`deepseek_frequency`
- 审计表 `deepseek_labels`（主键 `model + dataset_id + field_id`）记录原始响应、
  token 用量、重试次数、状态（`ok` / `invalid` / `error`）与提示词 sha256
- 视图 `v_deepseek_field_labels`：已打标签字段的扁平视图，便于下游取用

