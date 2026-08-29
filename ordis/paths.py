"""
数据目录统一入口：默认 ~/.ordis，设置 ORDIS_HOME 后重定向
（Docker 容器内挂卷 /data 即可持久化）。
"""

import os
from pathlib import Path


def data_home() -> Path:
    """Ordis 数据目录（shell 历史/模型配置/集群配置/技能库）。"""
    env = os.environ.get("ORDIS_HOME")
    if env:
        return Path(env)
    return Path.home() / ".ordis"
