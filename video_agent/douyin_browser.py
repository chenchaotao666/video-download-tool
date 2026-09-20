"""Playwright 浏览器兜底：yt-dlp 解不了抖音签名时的抓取通道。

背景：抖音 Web API（aweme/v1/web/aweme/detail 等）强制要求 a_bogus
动态签名，由混淆 JS 在浏览器里实时计算，yt-dlp 无法复现
（上游 issue: yt-dlp/yt-dlp#16803，官方暂无修复计划）。
但页面自己的 JS 会算签名——所以用真实浏览器打开页面，
拦截页面发出的接口响应，直接拿结构化数据，完全绕开 yt-dlp。

视频播放地址同样从响应里取（video.bit_rate / video.play_addr），
存进 VideoItem.formats 供用户选择画质，由 downloader 直连 CDN 下载。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

import config
from .models import VideoItem


def _browsers_path() -> str:
    """zip 分发版在 exe 旁自带 browsers/ 目录（免安装）；否则用用户级缓存目录。"""
    bundled = config.BASE_DIR / "browsers"
    if getattr(sys, "frozen", False) and bundled.is_dir():
        return str(bundled)
    return str(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
               / "ms-playwright")


# 打包成 exe 后 Playwright 驱动会到 exe 的临时解压目录里找浏览器，显式指定位置
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())

_DETAIL_API = "aweme/v1/web/aweme/detail/"

# 用真实 Chrome UA，和 cookie 导出时的浏览器环境保持一致
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _load_cookies(path: Path | None = None) -> list[dict]:
    """Netscape cookie 文件 -> Playwright add_cookies 格式。"""
    path = path or config.DOUYIN_COOKIE_FILE
    if not path.exists():
        return []
    cookies = []
    for line in path.read_text(encoding="utf-8").splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _sub, path, secure, expires, name, value = parts
        cookies.append({
            "name": name, "value": value,
            "domain": domain, "path": path,
            "expires": int(expires) if int(expires) > 0 else -1,
            "secure": secure == "TRUE",
            "httpOnly": http_only,
        })
    return cookies


def fetch_video(url: str) -> VideoItem | None:
    """用浏览器打开作品页，拦截 detail 接口响应，返回带画质列表的 VideoItem。

    作品链接和 v.douyin.com 短链都可以（短链会自动跳转）。
    先跑无头浏览器；被验证码风控时弹出真实浏览器窗口，
    用户手动过验证码后页面正常加载，接口拦截照样生效。
    """
    item = _run(url, headless=True, timeout=20000)
    if item:
        return item
    print("  [提示] 即将弹出浏览器窗口：如果出现滑块/验证码请手动完成，"
          "视频正常加载后会自动继续（最多等 2 分钟，窗口会自动关闭）……")
    return _run(url, headless=False, timeout=120000)


def _run(url: str, headless: bool, timeout: int) -> VideoItem | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "浏览器兜底需要 playwright：pip install playwright "
            "&& python -m playwright install chromium") from e

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:  # noqa: BLE001 - 浏览器没装/损坏时给可操作的中文提示
            raise RuntimeError(
                "Chromium 浏览器未安装或已损坏。\n"
                "请先运行: video-downloader.exe --install-browser"
                "（源码运行则是: python -m playwright install chromium）\n"
                f"原始错误: {e}") from e
        context = browser.new_context(
            user_agent=_UA, locale="zh-CN",
            viewport={"width": 1920, "height": 1080},
        )
        context.add_cookies(_load_cookies())
        page = context.new_page()
        try:
            return _fetch_video(page, url, timeout=timeout,
                                manual_captcha=not headless)
        except Exception as e:  # noqa: BLE001 - 兜底失败由调用方报"下载失败"
            print(f"  [提示] 浏览器兜底抓取失败: {e}")
            return None
        finally:
            browser.close()


def _fetch_video(page, url: str, timeout: int = 20000,
                 manual_captcha: bool = False) -> VideoItem | None:
    """打开作品页：优先拦截页面自己发出的 detail 接口响应；
    接口没触发（风控/验证码页）时退回解析页面内嵌的 SSR 数据。

    manual_captcha=True（有头窗口）时不做验证码提前退出——接口等待期间
    用户可以手动过验证码，页面跳转后 detail 接口照常触发。
    """
    try:
        with page.expect_response(
                lambda r: _DETAIL_API in r.url, timeout=timeout) as resp_info:
            page.goto(url, wait_until="domcontentloaded")
        detail = (resp_info.value.json() or {}).get("aweme_detail") or {}
        item = _detail_to_item(detail)
        # 图文帖没有 formats 但有 images，同样算成功
        if item and (item.formats or item.images):
            return item
    except Exception:  # noqa: BLE001 - 拦截失败走 SSR 解析，下面统一报错
        pass

    # 无头模式遇到风控直接报原因；有头模式继续等用户手动过验证码
    if not manual_captcha:
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        if "验证码" in title or "verify" in page.url:
            print(f"  [提示] 无头浏览器被验证码风控拦截（页面: {title}）。")
            return None

    print("  [提示] 未拦截到 detail 接口，尝试解析页面内嵌数据……")
    return _fetch_from_ssr(page)


def _fetch_from_ssr(page, timeout: int = 15000) -> VideoItem | None:
    """解析作品页 <script id="RENDER_DATA"> 里的 SSR 数据（URL 编码的 JSON）。

    抖音作品页的初始 HTML 内嵌了视频详情，即使接口被风控挡住，
    只要页面渲染出来就能拿到播放地址。
    """
    try:
        el = page.wait_for_selector("script#RENDER_DATA", state="attached",
                                    timeout=timeout)
        data = json.loads(unquote(el.inner_text()))
    except Exception as e:  # noqa: BLE001 - 兜底通道失败由调用方统一报"下载失败"
        print(f"  [提示] 页面内嵌数据解析失败: {e}")
        return None
    detail = _find_aweme_detail(data)
    return _detail_to_item(detail) if detail else None


def _find_aweme_detail(node, depth: int = 0) -> dict | None:
    """在 SSR JSON 里递归找视频详情：同时带 aweme_id 和 video/desc 的字典。"""
    if depth > 10 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, dict):
        if node.get("aweme_id") and ("video" in node or "desc" in node):
            return node
        children = node.values()
    else:
        children = node
    for child in children:
        found = _find_aweme_detail(child, depth + 1)
        if found:
            return found
    return None


def _list_streams(video: dict) -> list[dict]:
    """收集全部可用画质（bit_rate 列表 + 默认 play_addr），清晰度从高到低。

    play_addr 的 url_list 里 playwm 是水印版，换成 play 得无水印直链。
    每条: {"label", "url", "area", "h265"}。
    """
    entries: list[dict] = []
    seen: set[tuple] = set()

    def add(url_list, w, h, h265):
        urls = [u.replace("playwm", "play") for u in url_list or []
                if isinstance(u, str) and u.startswith("http")]
        if not urls:
            return
        key = (w, h, h265)
        if key in seen:
            return
        seen.add(key)
        label = f"{w}x{h}" if w and h else "默认画质"
        if h265:
            label += " H.265（慎选：Windows 自带播放器不支持，会只有声音）"
        entries.append({"label": label, "url": urls[0],
                        "area": w * h, "h265": h265})

    for br in video.get("bit_rate") or []:
        pa = br.get("play_addr") or {}
        add(pa.get("url_list"), pa.get("width") or 0, pa.get("height") or 0,
            bool(br.get("is_h265")))
    pa = video.get("play_addr") or {}
    add(pa.get("url_list"), pa.get("width") or 0, pa.get("height") or 0, False)

    # H.264 组在前、H.265 组在后，组内清晰度从高到低：
    # 抖音的 2K/4K 常常只有 H.265，默认选项必须落在 Windows 能播的 H.264 上
    entries.sort(key=lambda e: (e["h265"], -e["area"]))
    return entries


def _detail_to_item(detail: dict) -> VideoItem | None:
    vid = detail.get("aweme_id")
    if not vid:
        return None

    # 图文帖（aweme_type=68）：play_addr 是黑场占位视频，图片在 images 里
    images = []
    for img in detail.get("images") or []:
        for u in img.get("url_list") or []:
            if isinstance(u, str) and u.startswith("http"):
                images.append(u)
                break

    streams = [] if images else _list_streams(detail.get("video") or {})

    return VideoItem(
        platform="douyin",
        video_id=str(vid),
        title=detail.get("desc") or "",
        url=f"https://www.douyin.com/video/{vid}",
        direct_url=streams[0]["url"] if streams else "",
        images=images,
        formats=[{"label": s["label"], "url": s["url"]} for s in streams],
    )
