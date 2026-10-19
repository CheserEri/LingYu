# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把 lingyu 打成 Windows onedir 独立 exe。

请通过 ``packaging/build.py`` 调用。它会把本仓库内的
``packaging/wechat_bridge.py`` 作为数据文件内置进包，使分发的 exe 无需任何
外部目录即可运行。

体积说明：``wechatauto`` / ``wxauto4`` / ``uiautomation`` 会带进较多依赖；
``cv2`` / ``numpy`` 等仅在朋友圈等本适配层未启用的路径中被惰性导入，故剔除。
``imageio_ffmpeg``（自带 ffmpeg 二进制，约 84 MB）为「链接下载」合流 DASH
分离流所必需，``winrt``（winsdk 的继任者，约 40 MB）为 wechatauto 的 **OCR
路径**所必需——微信 4.1.15 等版本上 UIA 树不可用，此时 OCR 是唯一的会话定位
手段。二者均保留内置，会使产物体积明显增大。

内置 yt-dlp（``vendor/yt_dlp``，链接解析用）通过 ``pathex`` + ``hiddenimports``
交给 PyInstaller 做**静态分析**，并启用其自带的官方 hook（``vendor/yt_dlp/
__pyinstaller``）。这样 yt-dlp 用到的标准库子模块与全部提取器都会被自动收集；
早期把它仅当作数据目录分发时，必须手工枚举标准库，极易遗漏（实测先缺
``optparse``、补上后又缺 ``html.parser``）。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
BRIDGE = SPEC_DIR / "wechat_bridge.py"
WEBUI = ROOT / "src" / "lingyu" / "webui"
VENDOR = ROOT / "vendor"
# 应用图标（由 packaging/make_icon.py 生成），同时供 exe 与安装包使用。
ICON = SPEC_DIR / "lingyu.ico"

if not BRIDGE.is_file():
    raise SystemExit(
        "缺少 packaging/wechat_bridge.py；该文件应随仓库一同提供。"
    )
if not WEBUI.is_dir():
    raise SystemExit("缺少 src/lingyu/webui 前端目录。")
if not (VENDOR / "yt_dlp" / "__init__.py").is_file():
    raise SystemExit(
        "缺少 vendor/yt_dlp；内置的 yt-dlp 源码应随仓库一同提供。"
    )

datas = [
    (str(BRIDGE), "."),
    (str(WEBUI), "webui"),
    # 窗口图标：桌面端启动时显式传给 pywebview（与 exe 内嵌图标同一文件）。
    (str(ICON), "."),
]
binaries = []
hiddenimports = [
    # wechat_bridge 在运行期才被加载，需显式声明其依赖，否则冻结后报 ModuleNotFoundError。
    "loguru",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
    "win32gui",
    "win32ui",
    "win32api",
    "win32con",
    "win32process",
    "psutil",
    # 桌面 GUI（--gui 模式）
    "bottle",
    "webview",
    "webview.platforms.edgechromium",
    # 链接下载的 ffmpeg 定位（links.py 内部惰性导入，静态分析扫不到）
    "imageio_ffmpeg",
    # 内置 yt-dlp（链接解析）。显式声明后 PyInstaller 会真正分析 yt_dlp，
    # 自动收集它用到的全部标准库子模块（html.parser / urllib.request …）与
    # 940 个提取器；其官方 hook 另在下方 hookspath 中启用。
    "yt_dlp",
    # 抖音解析的浏览器桥：douyin.py 里是函数内惰性导入，静态分析未必扫到，
    # 漏掉会在冻结包里报 ModuleNotFoundError（只依赖标准库，不增加体积）。
    "lingyu.bot.browser",
    # WinRT OCR（wechatauto 里写死 `from winsdk...`，桥接层会注册别名）。
    # 这些子模块是命名空间包，静态分析扫不到，必须显式声明。
    "winrt",
    "winrt.windows",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media",
    "winrt.windows.media.ocr",
    "winrt.windows.storage",
    "winrt.windows.storage.streams",
]

# collect_all 对「当前解释器里没装」的包会**静默返回空**，产出一个看起来正常、
# 实际 OCR 全废的包——0.8.0 和 0.8.13 都踩过这个坑（运行时日志
# `发送消息失败: No module named 'winsdk'`）。这里先直接导入校验，缺了就中止，
# 别让残缺包流出去。
try:
    import winrt  # noqa: F401
    import winrt.windows.media.ocr  # noqa: F401
    import winrt.windows.storage  # noqa: F401
except ImportError as _ocr_exc:
    raise SystemExit(
        "OCR 依赖 winrt 缺失，拒绝构建："
        f"{_ocr_exc}\n"
        "缺它时 UIA 一旦失效就彻底发不出消息（日志只有一句 "
        "`No module named 'winsdk'`）。\n"
        '请先执行：pip install -e ".[wechat]"'
    ) from _ocr_exc

for _pkg in (
    "wechatauto",
    "wxauto4",
    "uiautomation",
    "comtypes",
    # pywebview（含其 js 资源）与 pythonnet（clr，含 Python.Runtime 二进制）
    "webview",
    "clr",
    # 链接下载：DASH 分离流（Bilibili/YouTube）必须靠 ffmpeg 合流，
    # imageio_ffmpeg 自带 ffmpeg 二进制（约 84 MB），故一并内置。
    "imageio_ffmpeg",
    # wechatauto 的 OCR 路径依赖 WinRT。**不能排除**：缺它时 OCR 会抛
    # ModuleNotFoundError，UIA 一旦失效就再也发不出消息（日志
    # `发送消息失败: No module named 'winsdk'`）。收集 winrt 而非 winsdk：
    # 后者已停更（wheel 只到 cp311），桥接层会把 winrt 注册成 winsdk 别名。
    "winrt",
):
    _datas, _binaries, _hidden = collect_all(_pkg)
    datas += _datas
    binaries += _binaries
    hiddenimports += _hidden

# 二次校验：确认 winrt 的文件真的进了产物清单。上面虽然导入过，
# 但 collect_all 在某些环境（如 site-packages 只读、hook 冲突）下仍可能
# 收不到文件，那时冻结包会在运行时才炸。
# 注意：pyd 会被 PyInstaller 归到 datas 而非 binaries，两边都要数。
_winrt_items = [
    item for item in (binaries + datas) if "winrt" in str(item[0]).lower()
]
if not _winrt_items:
    raise SystemExit(
        "winrt 收集到 0 个文件：OCR 在冻结包里不可用，"
        "UIA 失效时将彻底发不出消息。请检查 PyInstaller 版本与 "
        "site-packages 权限后重试。"
    )
print(f"[spec] winrt 相关文件已收集 {len(_winrt_items)} 个")

a = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT / "src"), str(VENDOR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # yt-dlp 自带的官方 PyInstaller hook（hook-yt_dlp.py），补齐可选依赖与兼容模块。
    hookspath=[str(VENDOR / "yt_dlp" / "__pyinstaller")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "cv2",
        "numpy",
        "pyautogui",
        "pypinyin",
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lingyu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lingyu",
)