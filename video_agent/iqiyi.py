"""爱奇艺抓取：移动端页面拿 tvid/vid，tmts 接口拿 m3u8 地址，ffmpeg 下 HLS。

流程：
1. 桌面播放页是 JS 空壳（拿不到 tvid），但移动端页面（m.iqiyi.com）的 HTML
   里内嵌 loadInfo（qipuId + vid + 时长 + 标题）；
2. 用 tvid/vid 调 cache.m.iqiyi.com/jp/tmts/ 接口（md5 签名，算法同 yt-dlp
   的 iqiyi 提取器，页面提取部分已失效但接口签名仍然有效）；
3. 响应 vidl 列表里每个清晰度一条 m3u8（m3utx 字段），ffmpeg 直下 HLS。

VIP/会员内容：匿名时 m3u8 带试看参数（prv=1&previewTime=…），
登录 cookie（www.iqiyi.com_cookies.txt 等）带上后返回完整播放列表。
"""

from __future__ import annotations

import json
import re
import time
from hashlib import md5

import config
from .models import VideoItem

_TMTS_KEY = "d5fb4bd9d50c4be6948c97edd7254b0e"   # tmts 接口签名密钥（同 yt-dlp）
_TMTS_SRC = "76f90cbd92f94a2e925d83e8ccd22cb7"
_REFERER = "https://www.iqiyi.com/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")

# 扩展导出时的文件名取决于导出时所在页面域名
_COOKIE_FILES = ("www.iqiyi.com_cookies.txt", "iqiyi.com_cookies.txt",
                 "v.iqiyi.com_cookies.txt")


def _http():
    """优先 curl_cffi（模拟 Chrome 的 TLS 指纹），没有则用 requests。"""
    try:
        from curl_cffi import requests as http
        return http, {"impersonate": "chrome"}
    except ImportError:
        import requests as http
        return http, {}


def _cookie_header() -> str:
    for name in _COOKIE_FILES:
        path = config.BASE_DIR / name
        if not (path.exists() and path.stat().st_size > 0):
            continue
        pairs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                pairs.append(f"{parts[5]}={parts[6]}")
        if pairs:
            return "; ".join(pairs)
    return ""


def has_cookie() -> bool:
    return bool(_cookie_header())


def _extract_vcode(url: str) -> str:
    """从链接里取视频代码（v_XXX / w_XXX 的 XXX 部分）。"""
    m = re.search(r"[vw]_([a-z0-9]+)\.html", url)
    return m.group(1) if m else ""


def _load_info(vcode: str) -> dict | None:
    """移动端页面内嵌的 loadInfo：tvid(qipuId)、vid、时长，外加页面标题。"""
    http, kwargs = _http()
    r = http.get(f"https://m.iqiyi.com/v_{vcode}.html",
                 headers={"User-Agent": _UA_MOBILE}, timeout=30, **kwargs)
    m = re.search(r'"loadInfo":\{(.{0,400}?)\}', r.text)
    if not m:
        return None
    try:
        info = json.loads("{" + m.group(1) + "}")
    except json.JSONDecodeError:
        return None
    if not info.get("qipuId") or not info.get("vid"):
        return None
    # 标题从 <title> 拿，去掉 "-爱奇艺" 等站点后缀
    t = re.search(r"<title>([^<]+)</title>", r.text)
    info["_title"] = re.split(r"[-_]", t.group(1))[0].strip() if t else ""
    return info


def _tmts(tvid: str, vid: str) -> dict | None:
    """调 tmts 接口拿播放数据（含各清晰度 m3u8）。"""
    tm = int(time.time() * 1000)
    sc = md5(f"{tm}{_TMTS_KEY}{tvid}".encode()).hexdigest()
    params = {"tvid": tvid, "vid": vid, "src": _TMTS_SRC, "sc": sc, "t": tm}
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    http, kwargs = _http()
    r = http.get(f"http://cache.m.iqiyi.com/jp/tmts/{tvid}/{vid}/",
                 params=params, headers=headers, timeout=30, **kwargs)
    t = r.text
    if t.startswith("var tvInfoJs="):
        t = t[len("var tvInfoJs="):]
    d = json.loads(t)
    if d.get("code") != "A00000":
        print(f"  [提示] 爱奇艺接口返回: {d.get('code')} {d.get('msg') or ''}")
        return None
    return d.get("data") or {}


def fetch_video(url: str) -> VideoItem | None:
    """解析视频信息和可选画质列表（formats 里带 m3u8 直链，选定即可下载）。"""
    vcode = _extract_vcode(url)
    if not vcode:
        print("  [提示] 没能从链接里解析出爱奇艺视频代码。")
        return None
    info = _load_info(vcode)
    if not info:
        print("  [提示] 解析视频信息失败（链接可能已失效）。")
        return None
    tvid, vid = str(info["qipuId"]), info["vid"]
    data = _tmts(tvid, vid)
    if not data:
        return None

    # 画质列表：vidl 按分辨率去重（同分辨率有多条，如不同音轨/编码）
    formats = []
    seen = set()
    for v in data.get("vidl") or []:
        res = v.get("screenSize") or ""
        m3u8 = v.get("m3utx") or ""
        if not res or not m3u8 or res in seen:
            continue
        seen.add(res)
        formats.append({"label": res, "m3u8": m3u8})
    formats.sort(key=lambda f: -int(f["label"].split("x")[1]))

    # 试看判定：m3u8 链接带 prv=1 / previewType 参数
    is_preview = any("prv=1" in f["m3u8"] or "previewType=1" in f["m3u8"]
                     for f in formats)

    return VideoItem(
        platform="iqiyi",
        video_id=vcode,
        title=info.get("_title") or "",
        url=f"https://www.iqiyi.com/v_{vcode}.html",
        formats=formats,
        is_preview=is_preview,
    )
