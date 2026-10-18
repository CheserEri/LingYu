"""用户数据目录定位。

配置、日志、下载缓存都放在用户私有目录下，而不是程序目录：安装版装在
``%LOCALAPPDATA%\\Programs\\lingyu``，源码模式则在仓库里，两处都不该写运行期数据。

统一入口只有 :func:`user_data_dir`，各模块不要再自己拼 ``%APPDATA%``。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "lingyu"


def user_data_dir() -> Path:
    """返回用户数据根目录（``%APPDATA%\\lingyu``）。

    ``APPDATA`` 缺失时（极少见，例如某些精简环境）退回 ``~/AppData/Roaming``，
    保证路径始终可用而不是抛异常。
    """
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_DIR_NAME


def logs_dir() -> Path:
    """返回运行日志目录（``%APPDATA%\\lingyu\\logs``）。"""
    return user_data_dir() / "logs"
