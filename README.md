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

