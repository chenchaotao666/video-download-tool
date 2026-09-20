#!/usr/bin/env python3
"""一键打包绿色版 exe：video-downloader.exe + 内嵌 ffmpeg + 自带 Chromium 浏览器。

用法：
    python build.py

环境要求：Windows + Python 3.10+，联网（首次要下载 ffmpeg 和 Chromium，共约 700MB）。
可重复运行：已下载的 ffmpeg / 浏览器会复用，只重建变动部分。

产物（整个 dist/ 目录打 zip 即是绿色免安装版）：
    dist/video-downloader.exe   主程序（内嵌 yt-dlp / curl_cffi / ffmpeg / Playwright 驱动）
    dist/browsers/              Chromium 浏览器全套（抖音功能用，用户免安装）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Windows 控制台默认 GBK，打印 emoji/中文提示会崩，强制 UTF-8 输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
BROWSERS_DIR = DIST_DIR / "browsers"
FFMPEG_BIN = BASE_DIR / "bin" / "ffmpeg.exe"

# ffmpeg Windows 官方构建（gyan.dev essentials 版，只取其中的 ffmpeg.exe）
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

EXE_NAME = "video-downloader"


def run(cmd: list[str], env: dict | None = None) -> None:
    """跑子进程，失败直接中止整个构建。"""
    print(f"$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, env=env)


def install_deps() -> None:
    """装项目依赖 + PyInstaller（已装的会自动跳过）。"""
    print("\n== 1/4 安装依赖 ==")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "pyinstaller"])


def ensure_ffmpeg() -> None:
    """保证 bin/ffmpeg.exe 存在：已有直接用 → PATH 里有就复制 → 都没有才下载。"""
    print("\n== 2/4 准备 ffmpeg ==")
    if FFMPEG_BIN.exists():
        print(f"已存在，跳过: {FFMPEG_BIN}")
        return
    on_path = shutil.which("ffmpeg")
    if on_path:
        FFMPEG_BIN.parent.mkdir(exist_ok=True)
        shutil.copy(on_path, FFMPEG_BIN)
        print(f"已从 PATH 复制: {on_path}")
        return

    print(f"下载 ffmpeg（约 110MB）: {FFMPEG_ZIP_URL}")
    FFMPEG_BIN.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "ffmpeg.zip"
        urllib.request.urlretrieve(FFMPEG_ZIP_URL, zpath)
        with zipfile.ZipFile(zpath) as z:
            member = next(n for n in z.namelist() if n.endswith("bin/ffmpeg.exe"))
            with z.open(member) as src, open(FFMPEG_BIN, "wb") as dst:
                shutil.copyfileobj(src, dst)
    print(f"已就位: {FFMPEG_BIN}")


def build_exe() -> None:
    """PyInstaller onefile 打包：内嵌 bin/ffmpeg.exe 和 Playwright 驱动。

    yt-dlp 自带 PyInstaller hook 会自动收集；playwright 需要 --collect-all
    把 driver（node.exe + cli.js）打进去，--install-browser 命令依赖它。
    """
    print("\n== 3/4 打包 exe ==")
    # 正在运行的实例会锁住 dist 里的旧 exe（Windows 不允许覆盖运行中的程序），
    # 打包前先把它们结束掉；没在跑就忽略报错
    subprocess.run(["taskkill", "/F", "/IM", f"{EXE_NAME}.exe"],
                   capture_output=True)
    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",           # --clean：清缓存，避免打进旧代码
        "--onefile", "--console",
        "--name", EXE_NAME,
        "--collect-all", "playwright",
        "--add-data", f"bin{os.pathsep}bin",  # 内嵌 ffmpeg（B 站分轨合并/HLS 用）
        "main.py",
    ])


def _browser_dirs() -> dict[str, str]:
    """当前 Playwright 版本要求的浏览器目录名：{浏览器名: chromium-1169 这样的目录名}。"""
    import playwright
    data = json.loads(
        (Path(playwright.__file__).parent / "driver" / "package" / "browsers.json")
        .read_text(encoding="utf-8"))
    dirs = {}
    for b in data["browsers"]:
        if b["name"] in ("chromium", "chromium-headless-shell", "ffmpeg"):
            dirs[b["name"]] = f"{b['name'].replace('-', '_')}-{b['revision']}"
    return dirs


def _copy_browsers_from_cache() -> None:
    """本机 Playwright 缓存（%LOCALAPPDATA%\\ms-playwright）里已有同版本浏览器就复制，
    省掉几百 MB 下载。只认带 INSTALLATION_COMPLETE 标记的完整安装。"""
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not cache.is_dir():
        return
    for name, dirname in _browser_dirs().items():
        src = cache / dirname
        dst = BROWSERS_DIR / dirname
        if dst.exists() or not (src / "INSTALLATION_COMPLETE").exists():
            continue
        print(f"复用本机缓存: {dirname}")
        shutil.copytree(src, dst)


def install_browsers() -> None:
    """把 Chromium 装进 dist/browsers/（exe 会优先用旁边的 browsers/，见 douyin_browser.py）。

    先复用本机缓存，缺的部分（通常是 chromium-headless-shell）再由
    playwright install 下载补齐；已齐全的重复运行会直接跳过。
    """
    print("\n== 4/4 安装 Chromium 到 dist/browsers ==")
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    _copy_browsers_from_cache()
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(BROWSERS_DIR))
    run([sys.executable, "-m", "playwright", "install", "chromium"], env=env)


def clean_dist() -> None:
    """清掉不该进安装包的测试产物 downloads/。cookie 文件保留不动
    （用户在 exe 旁边放的登录凭据，清理不该碰；分发前注意别打进去）。"""
    downloads = DIST_DIR / "downloads"
    if downloads.exists():
        shutil.rmtree(downloads)
        print(f"已清理: {downloads}")
    cookies = list(DIST_DIR.glob("*_cookies.txt"))
    if cookies:
        names = "、".join(c.name for c in cookies)
        print(f"[注意] dist 里有 cookie 文件（{names}），打 zip 分发前请取出，"
              "否则会泄露登录凭据。")


def main() -> int:
    if sys.platform != "win32":
        print("[错误] 这个打包脚本只支持 Windows（产物是 exe）。")
        return 1
    install_deps()
    ensure_ffmpeg()
    build_exe()
    install_browsers()
    clean_dist()
    print(f"""
打包完成 ✅ 产物在 {DIST_DIR}
    {EXE_NAME}.exe   主程序
    browsers\\        Chromium 浏览器（抖音功能免安装）

分发：整个 dist 目录压缩成 zip 即可，用户解压后双击 exe 直接使用。
提示：cookie 文件（*_cookies.txt）由最终用户自行导出放到 exe 旁边，不要打进包里。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
