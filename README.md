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
