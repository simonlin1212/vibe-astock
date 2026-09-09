"""Business data root; configure before starting API or CLI. Never moves data."""
from __future__ import annotations
import os
from pathlib import Path


def data_path(*parts: str) -> str:
    value = os.environ.get("ASTOCK_DATA_HOME") or "~/.duanxian-agents"
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("ASTOCK_DATA_HOME 必须是绝对路径；请指定完整磁盘目录")
    return str(root.joinpath(*parts))
