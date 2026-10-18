"""微信 UI 自动化桥接层。

``wechat_bridge.py`` 是本项目内置的桥接实现：运行期按**文件路径**加载，而不是
作为普通模块 import。这样做有两个好处：

* 桥接层依赖的自动化后端（UIA / OCR / 剪贴板）与主程序解耦，导入失败不会拖垮
  整个进程，错误能收敛成一条明确的「后端不可用」。
* 冻结打包后该文件以数据文件形式落在解包根目录，**可以单独替换来热更新桥接
  逻辑**，不必重新打包整个程序。

源码模式与冻结模式都从本包定位文件，不依赖任何外部项目目录。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from ..errors import BackendUnavailableError

BRIDGE_FILENAME = "wechat_bridge.py"
_MODULE_NAME = "wechat_bridge"


def bridge_file() -> Path:
    """返回桥接实现文件路径。

    冻结包内该文件由 PyInstaller 作为数据文件投放到解包根目录（``sys._MEIPASS``）；
    源码模式位于本包目录下。两条路径都由本函数统一给出，调用方不必关心模式。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / BRIDGE_FILENAME
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent / BRIDGE_FILENAME


def load_bridge_class() -> type:
    """加载并返回桥接层的 ``WeChatBridge`` 类。

    按文件路径加载而非 ``import``，避免污染 ``sys.path``，也让同一进程在替换
    文件后能拿到新实现。桥接层只依赖标准库与少量第三方包（loguru / PIL /
    pywin32 / psutil），不会连带导入本项目的其它模块。
    """
    module_file = bridge_file()
    if not module_file.is_file():
        raise BackendUnavailableError(
            f"缺少微信桥接模块: {module_file}",
            detail={"expected_file": str(module_file)},
        )

    spec = importlib.util.spec_from_file_location(_MODULE_NAME, module_file)
    if spec is None or spec.loader is None:
        raise BackendUnavailableError(
            f"无法加载微信桥接模块: {module_file}",
            detail={"module_file": str(module_file)},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # 自动化后端依赖未安装
        raise BackendUnavailableError(
            f"加载微信桥接模块失败，请确认自动化依赖已安装: {exc}",
            detail={"module_file": str(module_file)},
        ) from exc
    return module.WeChatBridge


__all__: list[str] = ["bridge_file", "load_bridge_class"]
