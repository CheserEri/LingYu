"""构建可分发的独立 exe（Windows onedir）、zip 与 Windows 安装包。

用法（仓库根目录）：

    .venv\\Scripts\\python.exe packaging\\build.py                # 全部产物
    .venv\\Scripts\\python.exe packaging\\build.py --no-installer  # 跳过安装包

产物：

    dist\\lingyu\\                     PyInstaller onedir 目录
    dist\\lingyu-win32-x64.zip         供 GitHub Release 与 npm 插件分发的压缩包
    dist\\lingyu-setup-x64.exe         Windows 安装包（需本机装有 Inno Setup 6）

前置：

* venv 中安装 PyInstaller：

      uv pip install pyinstaller --python .venv\\Scripts\\python.exe

* 生成安装包需要 Inno Setup 6（<https://jrsoftware.org/isdl.php>）的 ``ISCC.exe``。
  未安装时会**跳过**安装包并给出提示，不影响前两项产物。可用环境变量
  ``ISCC`` 指定编译器路径。

构建会把 ``src/lingyu/bridge/wechat_bridge.py`` 作为数据文件内置进包，
产物无需任何外部项目即可运行。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent
ROOT = SPEC_DIR.parent
BRIDGE = ROOT / "src" / "lingyu" / "bridge" / "wechat_bridge.py"
ISS = SPEC_DIR / "installer.iss"
ZIP_NAME = "lingyu-win32-x64.zip"
SETUP_NAME = "lingyu-setup-x64.exe"

# ISCC.exe 的常见位置（PATH 之外）。Inno Setup 允许装到任意目录，
# 所以这里只覆盖标准安装位置，其余情况请设环境变量 ISCC。
_ISCC_CANDIDATES = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe",
)


def check_bridge() -> None:
    """确认桥接层实现文件存在，供 spec 作为数据文件内置。"""
    if not BRIDGE.is_file():
        raise SystemExit(f"未找到 {BRIDGE}；该文件应随仓库一同提供。")
    print(f"[build] 将内置 wechat_bridge.py <- {BRIDGE}")


def read_version() -> str:
    """从 pyproject.toml 读版本号，用于安装包显示。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def run_pyinstaller() -> Path:
    dist = ROOT / "dist"
    work = ROOT / "build"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_DIR / "lingyu.spec"),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    out = dist / "lingyu"
    if not out.is_dir():
        raise SystemExit(f"打包失败，未生成目录: {out}")
    return out


def zip_dir(src: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src.parent))
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"[build] 已生成 {target} ({size_mb:.1f} MB)")


def _iscc_from_registry() -> Path | None:
    """从卸载信息里读 Inno Setup 的安装目录。

    Inno Setup 允许装到任意目录（不在 PATH 里、也不在 Program Files 下），
    靠固定候选路径会漏掉。它安装时会在卸载项里登记 ``InstallLocation``，
    这是唯一与安装位置无关的定位方式。
    """
    try:
        import winreg
    except ImportError:  # 非 Windows
        return None

    subkeys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey in subkeys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    location = winreg.QueryValueEx(key, "InstallLocation")[0]
            except OSError:
                continue
            candidate = Path(location) / "ISCC.exe"
            if candidate.is_file():
                return candidate
    return None


def find_iscc() -> Path | None:
    """定位 Inno Setup 的命令行编译器 ISCC.exe。

    顺序：环境变量 ``ISCC`` → PATH → 卸载信息登记的安装目录 → 常见默认目录。
    """
    override = os.environ.get("ISCC")
    if override and Path(override).is_file():
        return Path(override)
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    registered = _iscc_from_registry()
    if registered is not None:
        return registered
    for candidate in _ISCC_CANDIDATES:
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return path
    return None


def build_installer() -> None:
    """用 Inno Setup 编译 Windows 安装包。"""
    if not ISS.is_file():
        raise SystemExit(f"未找到安装脚本: {ISS}")
    iscc = find_iscc()
    if iscc is None:
        print(
            "[build] 未找到 ISCC.exe，跳过安装包。\n"
            "        安装 Inno Setup 6 后重跑，或用环境变量 ISCC 指定路径：\n"
            "        https://jrsoftware.org/isdl.php"
        )
        return
    version = read_version()
    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        str(ISS),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    setup = ROOT / "dist" / SETUP_NAME
    if not setup.is_file():
        raise SystemExit(f"安装包编译失败，未生成: {setup}")
    size_mb = setup.stat().st_size / 1024 / 1024
    print(f"[build] 已生成 {setup} ({size_mb:.1f} MB)")


def main() -> None:
    want_installer = "--no-installer" not in sys.argv
    check_bridge()
    out = run_pyinstaller()
    raw_size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[build] onedir 原始体积 {raw_size / 1024 / 1024:.1f} MB")
    zip_dir(out, ROOT / "dist" / ZIP_NAME)
    if want_installer:
        build_installer()


if __name__ == "__main__":
    main()
