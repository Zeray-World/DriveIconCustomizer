#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盘符图标设置工具
================
把任意一张图（ICO / PNG / JPG / BMP / WEBP）设成某个磁盘在资源管理器里的盘符图标。

如果目标盘是系统盘，会额外在图标左上角叠上一枚 Windows 徽标 —— 这枚徽标
不是内置图片，而是**运行时从本机系统盘的原装图标里现场抠出来的**，位置和
尺寸也按原图标实测比例自适应，所以换任何系统版本都不会跑偏。

用法
----
    python 设置盘符图标.py                      交互式菜单（推荐）
    python 设置盘符图标.py -d C -i 图.png       直接设置
    python 设置盘符图标.py -d C -i 图.png --no-logo   系统盘也不加徽标
    python 设置盘符图标.py -d C --restore       还原成系统默认图标
    python 设置盘符图标.py -d C -i 图.png --dry-run   只生成预览，不写注册表

说明
----
· 改图标要写 HKLM 注册表，因此需要管理员权限，脚本会自动请求提权。
· 只写 HKLM\\...\\Explorer\\DriveIcons\\<盘符>，不碰任何系统文件，随时可还原。
· 依赖 Pillow（pip install pillow）。
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import POINTER, byref, c_int, c_long, c_ubyte, c_void_p, c_wchar_p
from ctypes import wintypes

# --------------------------------------------------------------------------
# 依赖检查：Pillow 是唯一的第三方依赖
# --------------------------------------------------------------------------
try:
    from PIL import Image, ImageChops, ImageFilter
except ImportError:
    print("缺少 Pillow，请先执行：")
    print(f'    "{sys.executable}" -m pip install pillow')
    print("后再运行本脚本。")
    try:
        input("\n按回车退出…")
    except EOFError:
        pass
    sys.exit(1)


APP_NAME = "DriveIconCustomizer"

# 生成 ICO 时写进去的全部尺寸（资源管理器在小图标/中图标/大图标视图各取所需）
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
             (48, 48), (64, 64), (96, 96), (128, 128), (256, 256)]

ICO_SUFFIXES = (".ico", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif")
DRIVE_TYPES = {0: "未知", 1: "无效", 2: "可移动磁盘", 3: "本地磁盘",
               4: "网络驱动器", 5: "光驱", 6: "内存盘"}

REG_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\DriveIcons"


# ==========================================================================
# 路径（全部运行时推导，不写死盘符 / 用户名 / 安装位置）
# ==========================================================================
def _try_mkdir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def data_dir():
    """存放生成的 ico、徽标缓存和备份记录。

    依次尝试 ProgramData → LOCALAPPDATA → 用户目录 → 临时目录，
    哪个能写就用哪个，避免在受限环境里直接崩掉。
    """
    bases = [os.environ.get("ProgramData"),
             os.environ.get("LOCALAPPDATA"),
             os.path.expanduser("~")]
    for base in bases:
        if not base:
            continue
        d = os.path.join(base, APP_NAME)
        if _try_mkdir(d):
            return d

    d = os.path.join(tempfile.gettempdir(), APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def icons_dir():
    d = os.path.join(data_dir(), "icons")
    if _try_mkdir(d):
        return d
    d = os.path.join(tempfile.gettempdir(), APP_NAME, "icons")
    os.makedirs(d, exist_ok=True)
    return d


def write_ico_atomic(src_path, preferred, on_system_drive, want_badge, verbose=True):
    """先写临时文件再原子替换，避免中途失败把正在用的图标文件弄坏。

    替换失败（旧文件属主不同之类）就退到带时间戳的新文件名，绝不先删旧文件。
    返回 (最终路径, 256 画布)。
    """
    tmp = f"{preferred}.{os.getpid()}.tmp"
    try:
        canvas = build_ico(src_path, tmp, on_system_drive=on_system_drive,
                           want_badge=want_badge, verbose=verbose)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    try:
        os.replace(tmp, preferred)
        return preferred, canvas
    except OSError:
        pass

    alt = f"{os.path.splitext(preferred)[0]}_{int(time.time())}.ico"
    try:
        os.replace(tmp, alt)
        return alt, canvas
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def badge_cache_path():
    return os.path.join(data_dir(), "badge_cache.png")


def backup_path():
    return os.path.join(data_dir(), "backup.json")


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


# ==========================================================================
# Windows API —— 全部用 ctypes，不依赖 pywin32
# ==========================================================================
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", c_ubyte * 8)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", c_long), ("cy", c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", c_long), ("bmWidth", c_long), ("bmHeight", c_long),
                ("bmWidthBytes", c_long), ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD), ("bmBits", c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", c_long),
                ("biHeight", c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", c_long),
                ("biYPelsPerMeter", c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _guid(text):
    u = uuid.UUID(text)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    for i, byte in enumerate(u.bytes[8:]):
        g.Data4[i] = byte
    return g


IID_IShellItemImageFactory = _guid("bcc18b79-ba16-442f-80c4-8a59c30c463b")

_shell32.SHCreateItemFromParsingName.argtypes = [
    c_wchar_p, c_void_p, POINTER(_GUID), POINTER(c_void_p)]
_shell32.SHCreateItemFromParsingName.restype = c_long


def is_admin():
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(args):
    """以管理员身份重新启动自己（用 cmd /k 包一层，跑完窗口不闪退）。"""
    exe, script = sys.executable, os.path.abspath(__file__)
    tail = " ".join(f'"{a}"' for a in args)
    cmd = f'/k chcp 65001>nul & "{exe}" "{script}" {tail}'
    rc = _shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, script_dir(), 1)
    return rc > 32


def extract_shell_icon(path, size=256):
    """取某个路径在资源管理器里显示的真实图标（含系统叠加效果），返回 RGBA Image。"""
    _ole32.CoInitialize(None)
    item = c_void_p()
    hr = _shell32.SHCreateItemFromParsingName(
        path, None, byref(IID_IShellItemImageFactory), byref(item))
    if hr != 0:
        raise OSError(f"取图标失败（hr=0x{hr & 0xFFFFFFFF:08X}）：{path}")

    vtbl = ctypes.cast(item, POINTER(POINTER(c_void_p)))[0]
    GetImage = ctypes.WINFUNCTYPE(
        c_long, c_void_p, _SIZE, c_int, POINTER(wintypes.HBITMAP))(vtbl[3])

    hbmp = wintypes.HBITMAP()
    hr = GetImage(item, _SIZE(size, size), 0x4, byref(hbmp))  # SIIGBF_ICONONLY
    if hr != 0:
        raise OSError(f"渲染图标失败（hr=0x{hr & 0xFFFFFFFF:08X}）")

    bm = _BITMAP()
    _gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), byref(bm))

    bi = _BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = bm.bmWidth, -bm.bmHeight
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0

    buf = ctypes.create_string_buffer(bm.bmWidth * bm.bmHeight * 4)
    hdc = _user32.GetDC(None)
    _gdi32.GetDIBits(hdc, hbmp, 0, bm.bmHeight, buf, byref(bi), 0)
    _user32.ReleaseDC(None, hdc)
    _gdi32.DeleteObject(hbmp)

    img = Image.frombuffer("RGBA", (bm.bmWidth, bm.bmHeight), buf, "raw", "BGRA", 0, 1)
    if img.getchannel("A").getextrema()[1] == 0:
        img = img.convert("RGB").convert("RGBA")

    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])
    Release(item)
    return img


def list_volumes():
    """枚举本机所有带盘符的磁盘（不含光驱），返回结构化列表。"""
    out = []
    mask = _k32.GetLogicalDrives()
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        letter = chr(65 + i)
        root = f"{letter}:\\"
        dtype = _k32.GetDriveTypeW(c_wchar_p(root))
        if dtype not in (2, 3, 4):
            continue

        label = ""
        try:
            vol_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(261)
            if _k32.GetVolumeInformationW(
                    c_wchar_p(root), vol_buf, 261, None, None, None, fs_buf, 261):
                label = vol_buf.value
        except Exception:
            pass

        total = free = 0
        try:
            free_bytes = ctypes.c_ulonglong()
            total_bytes = ctypes.c_ulonglong()
            if _k32.GetDiskFreeSpaceExW(c_wchar_p(root), None,
                                        byref(total_bytes), byref(free_bytes)):
                total, free = total_bytes.value, free_bytes.value
        except Exception:
            pass

        out.append({
            "letter": letter,
            "root": root,
            "type": DRIVE_TYPES.get(dtype, "未知"),
            "label": label,
            "total": total,
            "free": free,
        })
    return out


def system_drive_letter():
    """本机系统盘盘符（单个大写字母），运行时推导，不写死。

    依次看 SystemDrive / SystemRoot / windir 环境变量；都被改坏时
    直接问 Windows 系统目录在哪，而不是假定 C 盘。
    """
    for var in ("SystemDrive", "SystemRoot", "windir"):
        val = os.environ.get(var, "")
        if not val:
            continue
        letter = os.path.splitdrive(val)[0].rstrip(":").upper()[:1]
        if "A" <= letter <= "Z":
            return letter

    try:
        buf = ctypes.create_unicode_buffer(260)
        if _k32.GetWindowsDirectoryW(buf, 260):
            letter = buf.value[:1].upper()
            if "A" <= letter <= "Z":
                return letter
    except Exception:
        pass
    return ""


def normalize_letter(raw):
    """把 'c' / 'C:' / ' c: ' 统一成 'C'；不是 A-Z 就返回 None。"""
    if not raw:
        return None
    text = str(raw).strip().strip('"').strip().rstrip(":\\/").strip()
    if not text:
        return None
    ch = text[0].upper()
    return ch if "A" <= ch <= "Z" else None


def short_path(path):
    """含空格的路径转 8.3 短路径，注册表里更稳妥；失败就原样返回。"""
    if " " not in path:
        return path
    buf = ctypes.create_unicode_buffer(1024)
    n = _k32.GetShortPathNameW(c_wchar_p(path), buf, 1024)
    return buf.value if 0 < n < 1024 else path


# ==========================================================================
# 文件选择对话框（原生，不依赖 tkinter）
# ==========================================================================
class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", c_wchar_p),
        ("lpstrCustomFilter", c_wchar_p), ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD), ("lpstrFile", c_wchar_p),
        ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", c_wchar_p),
        ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", c_wchar_p),
        ("lpstrTitle", c_wchar_p), ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", c_wchar_p), ("lCustData", wintypes.LPARAM),
        ("lpfnHook", c_void_p), ("lpTemplateName", c_wchar_p),
        ("pvReserved", c_void_p), ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD)]


def pick_file():
    """弹出系统"打开"对话框选一张图；取消返回 None。"""
    OFN_FILEMUSTEXIST, OFN_PATHMUSTEXIST = 0x00001000, 0x00000800
    OFN_EXPLORER, OFN_NOCHANGEDIR = 0x00080000, 0x00000008

    filt = ("图片 / 图标 (*.ico;*.png;*.jpg;*.jpeg;*.bmp;*.webp)\0"
            "*.ico;*.png;*.jpg;*.jpeg;*.bmp;*.webp\0"
            "所有文件 (*.*)\0*.*\0\0")
    buf = ctypes.create_unicode_buffer(1024)

    # 初始目录：优先脚本旁的素材目录，其次当前目录
    candidates = [os.path.join(script_dir(), "盘符图标素材"),
                  os.path.join(script_dir(), "ico"),
                  script_dir()]
    start = next((c for c in candidates if os.path.isdir(c)), script_dir())

    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.lpstrFilter = filt
    ofn.lpstrFile = ctypes.cast(buf, c_wchar_p)
    ofn.nMaxFile = 1024
    ofn.lpstrInitialDir = start
    ofn.lpstrTitle = "选择要用作盘符图标的图片"
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER | OFN_NOCHANGEDIR
    ofn.FlagsEx = 0

    try:
        _comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        _comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
        if _comdlg32.GetOpenFileNameW(byref(ofn)):
            return buf.value or None
    except Exception as e:
        print(f"  [提示] 无法打开文件选择框（{e}），请手动输入路径。")
    return None


# ==========================================================================
# 徽标：从系统盘原装图标里现场提取
# ==========================================================================
def _squarify(img, pad_ratio=0.02):
    """把任意图居中放进透明方形画布，保证转成盘符图标后不变形。

    pad_ratio 默认只留 2% 边距 —— 盘符图标在列表视图里是 16×16，
    留边太多会让图标显得比别人小一圈。
    """
    img = img.convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    pad = max(1, int(round(side * pad_ratio)))
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.alpha_composite(img, (pad + (side - w) // 2, pad + (side - h) // 2))
    return canvas


def find_badge(icon_rgba):
    """从一张盘符图标里找出 Windows 徽标。

    做法：徽标是高饱和蓝色，硬盘本体是低饱和灰，用「蓝通道比红绿通道高出多少」
    做软分割即可。边缘抗锯齿像素的 RGB 仍是蓝色（PNG 直通 alpha），所以掩膜
    不会吃掉边缘，抠出来的就是原图的真实 alpha。

    返回 (badge_image, (ratio_x, ratio_y, ratio_w, ratio_h))；找不到返回 (None, None)。
    """
    icon = icon_rgba.convert("RGBA")
    W, H = icon.size
    r, g, b, a = icon.split()

    # 蓝色优势 = B - max(R,G)。灰色硬盘这个值接近 0，徽标内部是 60+，
    # 之间线性过渡，边缘抗锯齿靠这个过渡自然保留。
    blueness = ImageChops.subtract(b, ImageChops.lighter(r, g))
    soft = blueness.point(lambda v: 0 if v <= 18 else (255 if v >= 60 else
                                                      int((v - 18) * 255 / 42)))
    opaque = a.point(lambda v: 255 if v > 8 else 0)
    mask = ImageChops.multiply(soft, opaque)
    mask = mask.filter(ImageFilter.MedianFilter(3))     # 去掉零星噪点

    bbox = mask.getbbox()
    if not bbox:
        return None, None
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0

    # 合理性校验：徽标该在左上、约占 2%~45% 面积、大致方
    if bw < W * 0.12 or bh < H * 0.12:
        return None, None
    if (bw * bh) / float(W * H) > 0.45:
        return None, None
    if x0 + bw / 2 > W * 0.55 or y0 + bh / 2 > H * 0.55:
        return None, None
    if not (0.6 <= bw / float(bh) <= 1.7):
        return None, None

    cut = icon.copy()
    cut.putalpha(ImageChops.multiply(icon.getchannel("A"), mask))
    badge = cut.crop(bbox)
    ratios = (x0 / W, y0 / H, bw / W, bh / H)
    return badge, ratios


def get_system_badge(verbose=True):
    """拿到系统盘徽标：先读缓存，缓存没有就从当前系统盘图标现场抠一份。"""
    cache = badge_cache_path()
    meta = cache + ".json"

    if os.path.isfile(cache) and os.path.isfile(meta):
        try:
            with open(meta, "r", encoding="utf-8") as f:
                info = json.load(f)
            badge = Image.open(cache).convert("RGBA")
            ratios = tuple(info["ratios"])
            if verbose:
                print(f"  使用已缓存的徽标：{badge.size[0]}×{badge.size[1]}px")
            return badge, ratios
        except Exception:
            pass  # 缓存坏了就重新抠

    letter = system_drive_letter()
    if verbose:
        print(f"  正在从 {letter}: 的系统图标里提取 Windows 徽标…")
    try:
        icon = extract_shell_icon(f"{letter}:\\", 256)
    except OSError as e:
        print(f"  [提示] 读取系统盘图标失败：{e}")
        return None, None

    badge, ratios = find_badge(icon)
    if badge is None:
        print("  [提示] 没能从系统盘当前图标里识别出 Windows 徽标。")
        print("        常见原因是系统盘图标已被替换过（替换后的图里")
        print("        徽标和图形混在一起，无法再分离），或该系统版本本身不带徽标。")
        print("        想让系统盘也带徽标：先用菜单 2 把系统盘还原，再重新设置一次。")
        return None, None

    try:
        badge.save(cache)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"ratios": list(ratios), "source": f"{letter}:\\",
                       "captured": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "size": list(badge.size)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if verbose:
        print(f"  徽标提取成功：{badge.size[0]}×{badge.size[1]}px，"
              f"位于原图标 ({ratios[0]*100:.1f}%, {ratios[1]*100:.1f}%)，"
              f"占宽 {ratios[2]*100:.1f}%")
    return badge, ratios


def compose_badge(base, badge, ratios):
    """按系统盘原版比例把徽标叠到 base 的左上角（base 应为 256 画布）。"""
    W, H = base.size
    rx, ry, rw, rh = ratios
    tw, th = max(1, int(round(rw * W))), max(1, int(round(rh * H)))
    scaled = badge.resize((tw, th), Image.LANCZOS)
    out = base.copy()
    out.alpha_composite(scaled, (int(round(rx * W)), int(round(ry * H))))
    return out


# ==========================================================================
# ICO 生成
# ==========================================================================
def build_ico(src_path, out_path, on_system_drive=False, want_badge=True,
              verbose=True):
    """把任意图片做成 10 个尺寸齐全的 ICO，可按需叠上系统盘徽标。"""
    src = Image.open(src_path)
    # 多帧格式（ico/gif）取最大的一帧
    if getattr(src, "n_frames", 1) > 1:
        best, best_area = None, -1
        for i in range(src.n_frames):
            src.seek(i)
            if src.size[0] * src.size[1] > best_area:
                best, best_area = src.copy(), src.size[0] * src.size[1]
        src = best

    # 全透明的图转成图标等于什么都没设，提前拦掉
    probe = src.convert("RGBA")
    if probe.getbbox() is None:
        raise ValueError("这张图整体是全透明的（或没有任何可见内容），"
                         "转成图标会看不见，请换一张。")

    # 已经做好的 ICO 保留下它自己的构图，只做尺寸对齐；
    # 普通图片才走「内容居中 + 留边」的规范化。
    if os.path.splitext(src_path)[1].lower() == ".ico":
        canvas = src.convert("RGBA")
        if canvas.size[0] != canvas.size[1]:
            canvas = _squarify(canvas)
        canvas = canvas.resize((256, 256), Image.LANCZOS)
    else:
        canvas = _squarify(src).resize((256, 256), Image.LANCZOS)

    if on_system_drive and want_badge:
        badge, ratios = get_system_badge(verbose=verbose)
        if badge is not None:
            canvas = compose_badge(canvas, badge, ratios)
        elif verbose:
            print("  → 本次不加徽标，直接使用原图。")

    canvas.save(out_path, format="ICO", sizes=ICO_SIZES)
    return canvas


def verify_ico(path):
    """回读校验 ICO 里实际写了哪些尺寸。"""
    with Image.open(path) as im:
        return sorted(im.info.get("sizes", []))


# ==========================================================================
# 注册表
# ==========================================================================
def reg_path(letter):
    return f"{REG_ROOT}\\{letter.upper()}\\DefaultIcon"


def read_current_icon(letter):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path(letter)) as k:
            return winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None


def write_icon(letter, ico_path):
    import winreg
    value = f'"{ico_path}"' if " " in ico_path else ico_path
    key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, reg_path(letter), 0,
                             winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)
    return value


def clear_icon(letter):
    import winreg
    removed = False
    for path in (reg_path(letter), REG_ROOT + f"\\{letter.upper()}"):
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, path)
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def load_backup():
    try:
        with open(backup_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_backup(data):
    try:
        with open(backup_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def refresh_shell():
    """让资源管理器立刻重读图标。

    先清掉图标缓存，再触发 shell 重新加载 —— 顺序反了的话等于清完没重建。
    两个命令都可能不存在（老系统），失败就忽略，后面还有 SHChangeNotify 兜底。
    """
    flags = 0x08000000  # CREATE_NO_WINDOW
    for cmd in (["ie4uinit.exe", "-ClearIconCache"], ["ie4uinit.exe", "-show"]):
        try:
            subprocess.run(cmd, creationflags=flags, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        _shell32.SHChangeNotify(0x08000000, 0x1000, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:
        pass


def _explorer_running():
    """True=在跑, False=确实没跑, None=判断不了。"""
    try:
        r = subprocess.run(
            ["tasklist", "/fi", "imagename eq explorer.exe", "/nh", "/fo", "csv"],
            creationflags=0x08000000, capture_output=True, timeout=10)
        text = (r.stdout or b"").decode("mbcs", "ignore").lower()
        if "explorer.exe" in text:
            return True
        return False if r.returncode == 0 else None
    except Exception:
        return None


def restart_explorer():
    """重启资源管理器。

    先确认真的杀掉了才动手拉起 —— 否则会在已有 explorer 上再开一个窗口；
    杀掉后 Windows 通常会自动重建 shell，所以先等它自己起来，等不到才手动拉。
    """
    try:
        r = subprocess.run(["taskkill", "/f", "/im", "explorer.exe"],
                           creationflags=0x08000000, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            return False
    except Exception:
        return False

    for _ in range(8):          # 最多等 4 秒
        time.sleep(0.5)
        if _explorer_running() is True:
            return True

    # 只有明确确认它没起来时才手动拉，判断不了就交给系统，绝不乱开窗口
    if _explorer_running() is False:
        try:
            subprocess.Popen(["explorer.exe"], creationflags=0x08000000)
        except Exception:
            return False
    return True


# ==========================================================================
# 交互
# ==========================================================================
def hr(char="-", n=62):
    print(char * n)


def choose_drive():
    vols = list_volumes()
    if not vols:
        print("没有找到可用磁盘。")
        return None
    sys_letter = system_drive_letter()

    print()
    print(f"  {'编号':<4}{'盘符':<6}{'类型':<12}{'可用 / 总容量':<22}{'卷标'}")
    hr()
    for i, v in enumerate(vols, 1):
        if v["total"]:
            size = f'{v["free"]/1024**3:.1f} / {v["total"]/1024**3:.1f} GB'
        else:
            size = "—"
        mark = "  ★ 系统盘" if v["letter"] == sys_letter else ""
        label = v["label"] or "—"
        print(f"  {i:<4}{v['letter'] + ':':<6}{v['type']:<12}{size:<22}{label}{mark}")
    hr()

    while True:
        raw = input("请输入要设置图标的磁盘编号（直接回车取消）：").strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(vols):
            return vols[int(raw) - 1]
        # 也允许直接输入盘符
        if raw[:1].upper() in [v["letter"] for v in vols]:
            return next(v for v in vols if v["letter"] == raw[:1].upper())
        print("  输入无效，请重新输入。")


def ask_yes(prompt, default=True):
    tip = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{tip}]：").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1")


def do_set(letter, img_path, want_badge, dry_run=False):
    """核心流程：生成 ICO → 写注册表 → 刷新。返回 True 表示成功。"""
    letter = normalize_letter(letter)
    if not letter:
        print("[错误] 盘符不合法，应为 A~Z 中的一个字母。")
        return False
    if not os.path.isfile(img_path):
        print(f"[错误] 找不到文件：{img_path}")
        return False
    if not img_path.lower().endswith(ICO_SUFFIXES):
        print(f"[错误] 不支持的格式：{os.path.splitext(img_path)[1]}")
        return False

    on_sys = letter.upper() == system_drive_letter()
    print()
    print(f"目标磁盘：{letter.upper()}:{'（系统盘，将保留 Windows 徽标）' if on_sys and want_badge else ''}")
    print(f"来源图片：{img_path}")

    if dry_run:
        # 预览模式写独立文件，绝不碰正在使用的那份图标
        out_dir = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(out_dir, f"preview_{letter}.ico")
    else:
        target = os.path.join(icons_dir(), f"drive_{letter}.ico")

    try:
        out_ico, canvas = write_ico_atomic(
            img_path, target, on_sys, want_badge, verbose=True)
    except Exception as e:
        print(f"[错误] 生成图标失败：{e}")
        return False

    sizes = verify_ico(out_ico)
    print(f"  已生成：{out_ico}")
    if sizes:
        print(f"  内含尺寸：{', '.join(f'{w}×{h}' for w, h in sizes)}")
    else:
        print("  [警告] 回读图标尺寸失败，但文件已写出。")

    if dry_run:
        preview = f"{os.path.splitext(out_ico)[0]}.png"
        canvas.save(preview)
        print(f"  [预览模式] 未写入注册表。预览图：{preview}")
        return True

    if not is_admin():
        print("[错误] 当前不是管理员，无法写入注册表。")
        return False

    old = read_current_icon(letter)
    if old:
        print(f"  该盘已有自定义图标，将被覆盖（原值已记录：{old}）")
        bk = load_backup()
        bk[letter.upper()] = {"value": old, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_backup(bk)

    try:
        value = write_icon(letter, short_path(out_ico))
    except Exception as e:
        print(f"[错误] 写入注册表失败：{e}")
        return False

    print(f"  注册表已写入：{value}")
    print("  正在刷新图标缓存…")
    refresh_shell()
    time.sleep(0.6)
    print()
    print("  ✔ 完成。打开「此电脑」看看效果；若还是旧图标，用菜单里的"
          "「重启资源管理器」或注销后重新登录。")
    return True


def do_restore(letter):
    """删掉该盘的自定义图标设置。顺手清掉可能残留的空键。"""
    letter = normalize_letter(letter)
    if not letter:
        print("[错误] 盘符不合法，应为 A~Z 中的一个字母。")
        return False
    if not is_admin():
        print("[错误] 当前不是管理员，无法修改注册表。")
        return False

    had = read_current_icon(letter) is not None
    removed = clear_icon(letter)

    if removed:
        print(f"  已删除 {letter}: 的自定义图标设置。")
        bk = load_backup()
        bk.pop(letter, None)
        save_backup(bk)
        refresh_shell()
        time.sleep(0.6)
        print("  ✔ 已还原为系统默认图标。" if had else "  ✔ 已清理残留设置。")
        return True

    if not had:
        print(f"  {letter}: 当前没有自定义图标，无需还原。")
        return True

    print("[错误] 删除注册表项失败（可能被系统保护或权限不足）。")
    return False


def run_set(letter, img_path, want_badge, dry_run=False):
    """需要管理员时自动带参数提权重放，避免用户重新选一遍盘和图。"""
    letter = normalize_letter(letter)
    if not letter:
        print("[错误] 盘符不合法，应为 A~Z 中的一个字母。")
        return False
    if dry_run or is_admin():
        return do_set(letter, img_path, want_badge, dry_run=dry_run)

    print()
    print("  写入注册表需要管理员权限，正在弹出授权窗口…")
    extra = ["-d", letter, "-i", os.path.abspath(img_path)]
    if not want_badge:
        extra.append("--no-logo")
    if relaunch_as_admin(extra):
        print("  已在新的管理员窗口中继续执行，请到那个窗口查看结果。")
        return True
    print("  [错误] 授权被取消，未做任何修改。")
    return False


def run_restore(letter):
    if is_admin():
        return do_restore(letter)

    print()
    print("  修改注册表需要管理员权限，正在弹出授权窗口…")
    if relaunch_as_admin(["-d", letter, "--restore"]):
        print("  已在新的管理员窗口中继续执行，请到那个窗口查看结果。")
        return True
    print("  [错误] 授权被取消，未做任何修改。")
    return False


def print_menu():
    print()
    hr("=", 62)
    print("            盘符图标设置工具")
    hr("=", 62)
    print("  1) 设置盘符图标")
    print("  2) 还原为系统默认图标")
    print("  3) 重启资源管理器（图标没刷新时用）")
    print("  0) 退出")
    hr()


# ==========================================================================
# main
# ==========================================================================
def main():
    ap = argparse.ArgumentParser(
        description="设置磁盘在资源管理器里的盘符图标（系统盘自动保留 Windows 徽标）")
    ap.add_argument("-d", "--drive", help="盘符，例如 C")
    ap.add_argument("-i", "--image", help="图标来源图片")
    ap.add_argument("--restore", action="store_true", help="还原为系统默认图标")
    ap.add_argument("--no-logo", action="store_true", help="系统盘也不叠加 Windows 徽标")
    ap.add_argument("--dry-run", action="store_true", help="只生成图标与预览，不写注册表")
    ap.add_argument("--no-admin", action="store_true", help="跳过自动提权（调试用）")
    args = ap.parse_args()

    # 命令行直通模式：参数齐全，可以提前提权，重放不会丢任何选择
    if (args.drive and not args.dry_run and not args.no_admin
            and not is_admin()):
        print("写入注册表需要管理员权限，正在请求提权…")
        if relaunch_as_admin(sys.argv[1:]):
            return 0
        print("[错误] 提权被取消，无法继续。")
        return 1

    # ---------- 命令行直通模式 ----------
    if args.drive:
        letter = normalize_letter(args.drive)
        if not letter:
            print(f"[错误] 盘符不合法：{args.drive!r}（应为 A~Z 中的一个字母）")
            return 2
        if args.restore:
            ok = do_restore(letter)
        else:
            img = args.image
            if not img:
                print("请用 -i 指定图片路径。")
                return 2
            ok = do_set(letter, os.path.abspath(img), not args.no_logo,
                        dry_run=args.dry_run)
        if sys.stdin and sys.stdin.isatty():
            try:
                input("\n按回车退出…")
            except EOFError:
                pass
        return 0 if ok else 1

    # ---------- 交互菜单 ----------
    while True:
        print_menu()
        choice = input("请选择操作：").strip()
        if choice == "0":
            print("再见。")
            return 0
        if choice == "1":
            vol = choose_drive()
            if not vol:
                continue
            letter = vol["letter"]
            on_sys = letter == system_drive_letter()

            print()
            print("即将打开文件选择框，请挑一张图（ICO / PNG / JPG / BMP / WEBP 都行）。")
            input("按回车打开…")
            img = pick_file()
            if not img:
                # 对话框取消，允许直接粘路径
                img = input("已取消。也可以直接把图片路径粘贴到这里（回车跳过）：").strip().strip('"')
                if not img:
                    print("  已跳过。")
                    continue

            want_badge = True
            if on_sys:
                want_badge = ask_yes("这是系统盘，要在左上角叠加 Windows 徽标吗？", True)

            run_set(letter, img, want_badge)
            continue

        if choice == "2":
            vol = choose_drive()
            if not vol:
                continue
            run_restore(vol["letter"])
            continue

        if choice == "3":
            print("正在重启资源管理器…")
            print("  ✔ 已重启。" if restart_explorer() else "  [提示] 请手动重启或注销后重试。")
            continue

        print("  没有这个选项。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
