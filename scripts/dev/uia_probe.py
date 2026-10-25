"""UIA 树探测：检查微信主窗口的 UIA 控件是否物化。

用法：
    python build/uia_probe.py           # 只探测
    python build/uia_probe.py --deep    # 打印前 N 层控件树
"""

from __future__ import annotations

import ctypes
import sys

import comtypes.client
from comtypes.gen import UIAutomationClient as UIA


SPI_GETSCREENREADER = 0x46
SPI_SETSCREENREADER = 0x47

CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"


def get_screen_reader_flag() -> bool:
    value = ctypes.c_int(0)
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENREADER, 0, ctypes.byref(value), 0
    )
    if not ok:
        return False
    return bool(value.value)


def set_screen_reader_flag(enable: bool = True) -> bool:
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETSCREENREADER, 1 if enable else 0, None, 0
    )
    return bool(ok)


def create_uia():
    return comtypes.client.CreateObject(CLSID_CUIAutomation, interface=UIA.IUIAutomation)


def find_wechat_hwnds() -> list[tuple[int, str, str]]:
    """返回 (hwnd, 类名, 标题) 列表。"""
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    results: list[tuple[int, str, str]] = []
    pids = set()

    # 找 Weixin.exe / WeChat.exe 进程
    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _proc(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if title and ("微信" in title or "WeChat" in title or "Weixin" in title):
            pids.add(pid.value)
            results.append((hwnd, cls, title))
        return True

    user32.EnumWindows(EnumWindowsProc(_proc), 0)
    return results


def dump_tree(el, depth: int, max_depth: int, indent: int = 0) -> int:
    """递归打印控件树，返回节点数。"""
    count = 1
    if depth >= max_depth:
        return count
    try:
        walker = create_uia().RawViewWalker
        child = walker.GetFirstChildElement(el)
        while child is not None:
            try:
                name = child.CurrentName or ""
                ctrl = child.CurrentControlType
                auto_id = child.CurrentAutomationId or ""
                cls = child.CurrentClassName or ""
                print(
                    f"{'  ' * (indent + 1)}- [{ctrl}] name={name!r} "
                    f"cls={cls!r} id={auto_id!r}"
                )
                count += dump_tree(child, depth + 1, max_depth, indent + 1)
            except Exception as exc:  # noqa: BLE001
                print(f"{'  ' * (indent + 1)}! 读取失败: {exc}")
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:  # noqa: BLE001
                break
    except Exception as exc:  # noqa: BLE001
        print(f"{'  ' * indent}! 遍历失败: {exc}")
    return count


def main() -> int:
    deep = "--deep" in sys.argv

    if "--set-flag" in sys.argv:
        print(f"[0] 设置读屏标志 = {set_screen_reader_flag(True)}")
    print(f"[1] 系统读屏标志 SPI_GETSCREENREADER = {get_screen_reader_flag()}")
    print(f"[2] UiaClientsAreListening = {bool(ctypes.windll.uiautomationcore.UiaClientsAreListening())}")

    windows = find_wechat_hwnds()
    print(f"[3] 找到 {len(windows)} 个微信相关窗口")
    for hwnd, cls, title in windows:
        print(f"    hwnd={hwnd} cls={cls!r} title={title!r}")

    if not windows:
        print("!! 没找到微信窗口，微信是否已启动并登录？")
        return 2

    uia = create_uia()

    best = 0
    for hwnd, cls, title in windows:
        try:
            el = uia.ElementFromHandle(hwnd)
        except Exception as exc:  # noqa: BLE001
            print(f"[4] ElementFromHandle({hwnd}) 失败: {exc}")
            continue

        try:
            name = el.CurrentName
            ctrl = el.CurrentControlType
            root_cls = el.CurrentClassName
        except Exception as exc:  # noqa: BLE001
            print(f"[4] hwnd={hwnd} 根节点属性读取失败: {exc}")
            continue

        walker = uia.RawViewWalker
        children = []
        child = walker.GetFirstChildElement(el)
        while child is not None:
            try:
                children.append(
                    (
                        child.CurrentControlType,
                        child.CurrentName or "",
                        child.CurrentClassName or "",
                    )
                )
            except Exception:  # noqa: BLE001
                children.append(("?", "?", "?"))
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:  # noqa: BLE001
                break

        best = max(best, len(children))
        print(
            f"[4] hwnd={hwnd} root=({ctrl},{name!r},{root_cls!r}) "
            f"一级子控件={len(children)}"
        )
        for c in children[:12]:
            print(f"      - {c}")

        if deep and children:
            print("[5] 控件树（深度 5）:")
            print(f"  * root ({name!r})")
            total = dump_tree(el, 0, 5, 0) + 1
            print(f"  节点总数(<=5层) = {total}")

    if best == 0:
        print("\n结论：UIA 树仍为空 —— 控件未物化")
        return 1

    print(f"\n结论：UIA 树已物化（最大一级子控件数 = {best}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
