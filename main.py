#!/usr/bin/env python3
"""单视频下载工具：给一个抖音 / B 站 / 腾讯视频链接，下载到 downloads/<平台>/。

用法：
    python main.py "https://www.douyin.com/video/xxx"      # 抖音作品链接
    python main.py "https://v.douyin.com/xxx/"             # 抖音分享短链
    python main.py "https://www.bilibili.com/video/BVxxx"  # B 站视频
    python main.py "https://v.qq.com/x/page/xxx.html"      # 腾讯视频
    python main.py                                         # 粘贴模式：循环粘贴链接，
                                                           # 不用加引号，直接回车退出
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config

# Windows 控制台默认 GBK，打印中文标题会崩或乱码，强制 UTF-8 输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from video_agent import downloader
from video_agent.models import VideoItem


def extract_url(text: str) -> str:
    """从输入里提取视频链接。

    支持直接粘贴抖音 App 的分享口令（链接夹在中文宣传语中间），
    自动抓出其中的 http(s) 链接；输入本身就是链接时原样返回。
    """
    text = text.strip().strip('"').strip("'")
    m = re.search(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", text)
    return m.group(0) if m else text


def detect_platform(url: str) -> str | None:
    if "douyin.com" in url:
        return "douyin"
    if "bilibili.com" in url or "b23.tv" in url:
        return "bilibili"
    if "v.qq.com" in url or "wetv.vip" in url:
        return "tencent"
    if "youku.com" in url or "soku.com" in url:
        return "youku"
    if "iqiyi.com" in url or "iq.com" in url:
        return "iqiyi"
    return None


def normalize_douyin_url(url: str) -> str:
    """把抖音个人主页弹窗链接（/user/self?...&modal_id=<作品id>）转成标准作品链接。

    从「我的收藏」等页面复制出来的链接带 modal_id 参数，浏览器通道
    只认 /video/<id> 形式，这里提取 modal_id 做归一化。
    """
    modal_id = parse_qs(urlparse(url).query).get("modal_id", [""])[0]
    if modal_id.isdigit():
        return f"https://www.douyin.com/video/{modal_id}"
    return url


def resolve_video_id(platform: str, url: str) -> str:
    """尽量不调签名接口就拿到视频 ID：链接里直接解析；抖音短链 HEAD 跟随跳转。"""
    if platform == "bilibili":
        m = re.search(r"(BV[0-9A-Za-z]+)", url)
        return m.group(1) if m else ""
    if platform == "tencent":
        m = (re.search(r"/x/(?:page|cover)/(?:[a-z0-9]+/)?([a-z0-9]+)\.html", url)
             or re.search(r"[?&]vid=([a-z0-9]+)", url))
        return m.group(1) if m else ""
    if platform == "youku":
        m = re.search(r"id_([A-Za-z0-9]+={0,2})\.html", url)
        return m.group(1) if m else ""
    if platform == "iqiyi":
        m = re.search(r"[vw]_([a-z0-9]+)\.html", url)
        return m.group(1) if m else ""
    m = re.search(r"modal_id=(\d+)", url) or re.search(r"/video/(\d+)", url)
    if m:
        return m.group(1)
    if "v.douyin.com" in url:
        try:
            try:
                from curl_cffi import requests as http
                kwargs = {"impersonate": "chrome"}
            except ImportError:
                import requests as http
                kwargs = {}
            # 风控时段 HEAD 可能拿不到跳转，退回 GET（不读 body，只看最终 URL）
            for method in (http.head, http.get):
                try:
                    r = method(url, allow_redirects=True, timeout=15, **kwargs)
                except Exception:  # noqa: BLE001 - 换个方法再试
                    continue
                m = re.search(r"/video/(\d+)", r.url)
                if m:
                    return m.group(1)
        except Exception:  # noqa: BLE001 - 解析不出就跳过本地查重，直接下载
            pass
    return ""


def find_existing(platform: str, video_id: str):
    """本地已有该视频的文件（文件名含 [视频ID]）则返回路径，避免重复下载。

    重复请求同一视频正是触发抖音验证码风控的常见原因，能跳过就跳过。
    """
    if not video_id:
        return None
    outdir = config.DOWNLOAD_DIR / platform
    if not outdir.is_dir():
        return None
    marker = f"[{video_id}]"
    for p in outdir.iterdir():
        # 只认成品：排除 yt-dlp 的 .part 临时文件和 .fXXX 分轨中间文件
        if marker not in p.name:
            continue
        if p.is_dir() and any(p.iterdir()):
            return p
        if p.suffix == ".mp4" and not re.search(r"\.f\d+\.", p.name):
            return p
    return None


def choose_format(options: list[dict]) -> dict:
    """打印可选画质让用户选；直接回车默认第 1 项（最高画质）。"""
    if len(options) <= 1:
        return options[0]
    print("   可选画质:")
    for i, o in enumerate(options, 1):
        print(f"     {i}. {o['label']}")
    try:
        s = input("   输入编号选择（直接回车 = 1）: ").strip()
    except EOFError:
        s = ""
    if s.isdigit() and 1 <= int(s) <= len(options):
        return options[int(s) - 1]
    if s:
        print("   输入无效，使用最高画质。")
    return options[0]


def download_one(raw_input: str) -> bool:
    """下载单个链接/分享口令，成功返回 True。"""
    url = extract_url(raw_input)
    if url != raw_input.strip().strip('"').strip("'"):
        print(f"   [提示] 从分享文本中提取到链接: {url}")
    if not url:
        print("[错误] 没有输入链接。")
        return False

    platform = detect_platform(url)
    if not platform:
        print(f"[错误] 不支持的平台: {url}")
        print("只支持抖音（douyin.com / v.douyin.com）、B 站（bilibili.com / b23.tv）、"
              "腾讯视频（v.qq.com）、优酷（youku.com）和爱奇艺（iqiyi.com）。")
        return False

    if platform == "douyin":
        normalized = normalize_douyin_url(url)
        if normalized != url:
            print(f"   [提示] 识别到 modal_id 弹窗链接，转为标准作品链接: {normalized}")
            url = normalized

    print(f">> 下载 [{platform}] {url}")

    # 本地已有该视频就直接跳过：重复请求同一视频正是触发抖音风控的常见原因
    existing = find_existing(platform, resolve_video_id(platform, url))
    if existing:
        print(f"   [提示] 该视频已下载过，跳过: {existing}")
        print("   如需重新下载，请先删除这个文件。")
        return True

    # 抖音不走 yt-dlp（Web API 强制 a_bogus 签名，直连频繁被 403/验证码风控），
    # 直接用 Playwright 浏览器通道：拦截 detail 接口拿到全部画质的无水印 CDN 直链，
    # 用户选定画质后直连下载；图文帖则逐张保存图片到文件夹。
    # 被验证码拦时会弹出真实浏览器窗口，手动过验证后自动继续
    if platform == "douyin":
        from video_agent import douyin_browser
        item = douyin_browser.fetch_video(url)
        ok = False
        if item and item.images:
            ok = downloader.download(item)
        elif item and item.formats:
            item.direct_url = choose_format(item.formats)["url"]
            ok = downloader.download(item)
        if ok:
            print(f"完成，文件在 {config.DOWNLOAD_DIR}/douyin/")
            return True
        print("[错误] 下载失败。若反复被验证码拦截，请重新导出 www.douyin.com_cookies.txt（见 README）。")
        return False

    # 腾讯视频：调 getvinfo 接口拿 vkey 签名直链（网页播放器是 fMP4 分段流，
    # 不好直接下，getvinfo 老接口仍返回整段 mp4 直链）；长视频分段则 ffmpeg 无损拼接。
    # 登录 cookie（v.qq.com_cookies.txt / www.qq.com_cookies.txt）存在时自动带上
    if platform == "tencent":
        from video_agent import tencent
        item = tencent.fetch_video(url)
        if not item or not item.formats:
            print("[错误] 解析失败。若是 VIP/权限限制，请导出 v.qq.com 的登录 cookie（见 README）。")
            return False
        # 剧集主页链接在 fetch_video 里才解析出真实 vid，这里补一次查重
        existing = find_existing(platform, item.video_id)
        if existing:
            print(f"   [提示] 该视频已下载过，跳过: {existing}")
            print("   如需重新下载，请先删除这个文件。")
            return True
        if item.is_preview:
            # VIP/付费内容：getinfo 只给试看，改用 h5vv6 getvinfo（ckey 签名 +
            # 登录 cookie）拿明文 vinfo，清晰度列表更全（可到 1080P/4K）。
            # 试看判定在 fetch_video 里已做，只弹这一次选择
            if not tencent.has_cookie():
                print("[错误] 这是 VIP/付费内容，请先导出 v.qq.com 的登录 cookie（见 README）。")
                return False
            cid = tencent.extract_cid(url)
            qualities = tencent.fetch_vip_qualities(item.video_id, cid, url)
            if not qualities:
                print("[错误] 没拿到 VIP 播放数据。请确认 cookie 未过期且账号有 VIP 权限（见 README）。")
                return False
            choice = choose_format(qualities)
            m3u8 = tencent.resolve_vip_hls(item.video_id, cid, url, choice["defn"])
            if not m3u8:
                print("[错误] 没拿到播放地址。")
                return False
            item.direct_url = m3u8
        else:
            choice = choose_format(item.formats)
            urls = tencent.resolve_stream(item.video_id, choice["defn"])
            if not urls:
                print("[错误] 没拿到播放地址。若是 VIP/权限限制，请导出 v.qq.com 的登录 cookie（见 README）。")
                return False
            if len(urls) == 1:
                item.direct_url = urls[0]
            else:
                item.segment_urls = urls
        if downloader.download(item):
            print(f"完成，文件在 {config.DOWNLOAD_DIR}/tencent/")
            return True
        print("[错误] 下载失败。")
        return False

    # 优酷：ups 接口拿分段 mp4 直链（ccode=0530；yt-dlp 自带提取器的 ccode 已失效），
    # 多音轨同分辨率优先国语，分段下载后 ffmpeg 无损拼接
    if platform == "youku":
        from video_agent import youku
        item = youku.fetch_video(url)
        if not item or not item.formats:
            print("[错误] 解析失败。需要登录的视频请导出 www.youku.com_cookies.txt（见 README）。")
            return False
        # video?s= 分享链接在 fetch_video 里才解析出真实 vid，这里补一次查重
        existing = find_existing(platform, item.video_id)
        if existing:
            print(f"   [提示] 该视频已下载过，跳过: {existing}")
            print("   如需重新下载，请先删除这个文件。")
            return True
        choice = choose_format(item.formats)
        urls = youku.resolve_stream(item.video_id, choice["stream_index"])
        if not urls:
            print("[错误] 没拿到播放地址。需要登录的视频请导出 www.youku.com_cookies.txt（见 README）。")
            return False
        item.segment_urls = urls
        if downloader.download(item):
            print(f"完成，文件在 {config.DOWNLOAD_DIR}/youku/")
            return True
        print("[错误] 下载失败。需要登录的视频请导出 www.youku.com_cookies.txt（见 README）。")
        return False

    # 爱奇艺：移动端页面拿 tvid/vid，tmts 接口拿各清晰度 m3u8，ffmpeg 下 HLS。
    # VIP 内容匿名只能试看（m3u8 带 prv=1 试看参数），需登录 cookie
    if platform == "iqiyi":
        from video_agent import iqiyi
        # 移动端页面 + tmts 接口两次请求，慢网络要几秒，先给个提示免得界面像卡住
        print("   正在解析爱奇艺视频信息……", flush=True)
        item = iqiyi.fetch_video(url)
        if not item or not item.formats:
            print("[错误] 解析失败。需要登录的视频请导出 www.iqiyi.com_cookies.txt（见 README）。")
            return False
        if item.is_preview and not iqiyi.has_cookie():
            print("[错误] 这是 VIP/付费内容，请先导出 www.iqiyi.com_cookies.txt（见 README）。")
            return False
        if item.is_preview:
            print("  [提示] VIP/付费内容，已带登录 cookie。")
        existing = find_existing(platform, item.video_id)
        if existing:
            print(f"   [提示] 该视频已下载过，跳过: {existing}")
            print("   如需重新下载，请先删除这个文件。")
            return True
        choice = choose_format(item.formats)
        item.direct_url = choice["m3u8"]
        if downloader.download(item):
            print(f"完成，文件在 {config.DOWNLOAD_DIR}/iqiyi/")
            return True
        print("[错误] 下载失败。需要登录的视频请导出 www.iqiyi.com_cookies.txt（见 README）。")
        return False

    # B 站：先探测可选画质让用户挑，再按选定格式下载；探测失败按默认策略下
    item = VideoItem(platform=platform, video_id="", title="", url=url)
    format_spec = None
    try:
        options = downloader.probe_ytdlp(url, platform)
        if options:
            format_spec = choose_format(options)["format"]
    except Exception as e:  # noqa: BLE001 - 探测失败不阻塞下载
        print(f"  [提示] 获取画质列表失败（{e}），按默认画质下载。")
    if downloader.download(item, format_spec):
        print(f"完成，文件在 {config.DOWNLOAD_DIR}/{platform}/")
        return True

    if platform == "youku":
        print("[错误] 下载失败。需要登录的优酷视频请导出 www.youku.com_cookies.txt（见 README）。")
    else:
        print("[错误] 下载失败。B 站 412/格式不可用时，请导出 www.bilibili.com_cookies.txt（见 README）。")
    return False


def install_browser() -> int:
    """下载 Playwright Chromium（换电脑后首次用抖音功能前跑一次）。

    exe 里内嵌的是 Playwright 驱动，浏览器本体太大不打进去，
    用这个命令装到用户级缓存目录（%LOCALAPPDATA%\\ms-playwright）。
    """
    from video_agent import douyin_browser  # noqa: F401 - 模块导入时设置浏览器缓存路径

    if getattr(sys, "frozen", False):
        driver = Path(sys._MEIPASS) / "playwright" / "driver"
    else:
        import playwright
        driver = Path(playwright.__file__).parent / "driver"
    node = driver / ("node.exe" if os.name == "nt" else "node")
    cli = driver / "package" / "cli.js"
    print("下载 Playwright Chromium 浏览器（约 150MB）……")
    return subprocess.run([str(node), str(cli), "install", "chromium"]).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音 / B 站单视频下载")
    parser.add_argument("url", nargs="?",
                        help="视频链接（douyin.com / v.douyin.com / bilibili.com / b23.tv）。"
                             "不传则进入粘贴模式，免去 PowerShell 里 & 要加引号的麻烦")
    parser.add_argument("--install-browser", action="store_true",
                        help="下载抖音功能所需的浏览器（换电脑后首次使用跑一次）")
    args = parser.parse_args()

    if args.install_browser:
        return install_browser()

    if args.url:
        return 0 if download_one(args.url) else 1

    # 粘贴模式：循环接收链接，直接回车退出（exe 双击打开就是这个模式）
    print("抖音 / B 站 / 腾讯 / 优酷 / 爱奇艺视频下载")
    print(f"下载目录: {config.DOWNLOAD_DIR}")
    while True:
        try:
            url = input("\n粘贴视频链接或分享口令后回车（直接回车退出）: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not url:
            break
        try:
            download_one(url)
        except Exception:  # noqa: BLE001 - 单个链接出错不退出，窗口保持打开
            import traceback
            traceback.print_exc()
            print("[错误] 这条链接下载出错，可以继续粘贴下一条。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - exe 双击运行时窗口会闪退，这里兜底打印并暂停
        import traceback
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            input("\n出错了，按回车退出……")
        sys.exit(1)
