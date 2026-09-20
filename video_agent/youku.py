"""优酷抓取：ups.get.json 接口拿 HLS 地址，yt-dlp 负责下载。

为什么不用 yt-dlp 自带的优酷提取器：它用的客户端标识 ccode=0564 已被服务端
判定为需登录（-3007），其他常见 ccode 又会触发账号风控（-6004 违规提示）；
实测 ccode=0530 目前对匿名和登录用户都可用。接口变化频繁，失效时先换 ccode 试试。

清晰度列表来自 data.stream；多音轨（国语/粤语等）同分辨率优先国语。
登录/会员内容需要 cookie（www.youku.com_cookies.txt，浏览器扩展导出）。
"""

from __future__ import annotations

import re
import time

import config
from .models import VideoItem

_API = "https://ups.youku.com/ups/get.json"
_CCODE = "0530"
_REFERER = "https://v.youku.com/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 扩展导出时的文件名取决于导出时所在页面，几个常见名字都认
_COOKIE_FILES = ("www.youku.com_cookies.txt", "youku.com_cookies.txt",
                 "v.youku.com_cookies.txt")

# 音轨代码 -> 中文名
_LANG_NAMES = {"guoyu": "国语", "yue": "粤语", "tspl": "台配", "english": "英语",
               "default": ""}


def _http():
    """优先 curl_cffi（模拟 Chrome 的 TLS 指纹），没有则用 requests。"""
    try:
        from curl_cffi import requests as http
        return http, {"impersonate": "chrome"}
    except ImportError:
        import requests as http
        return http, {}


def _load_cookie() -> tuple[str, str]:
    """读 cookie 文件，返回 (Cookie 请求头, cna 值)。没有则返回空串。"""
    for name in _COOKIE_FILES:
        path = config.BASE_DIR / name
        if not (path.exists() and path.stat().st_size > 0):
            continue
        pairs, cna = [], ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                pairs.append(f"{parts[5]}={parts[6]}")
                if parts[5] == "cna":
                    cna = parts[6]
        if pairs:
            return "; ".join(pairs), cna
    return "", ""


def _anonymous_cna() -> str:
    """匿名时的 utid：mmstat 的 eg.js 在 etag 里下发设备标识（同 yt-dlp 的做法）。"""
    try:
        http, kwargs = _http()
        r = http.get("https://log.mmstat.com/eg.js", timeout=15, **kwargs)
        return r.headers.get("etag", "").strip('"')
    except Exception:  # noqa: BLE001 - 拿不到就用占位，匿名免费视频不校验
        return ""


def extract_vid(url: str) -> str:
    """从优酷链接提取 vid（id_XXXX== 形式的视频 ID）。

    支持 v_show/id_XXX==.html 和 video?s= 分享链接（跟随跳转拿真实地址）。
    """
    m = re.search(r"id_([A-Za-z0-9]+={0,2})", url)
    if m:
        return m.group(1)
    if "video?s=" in url:
        try:
            http, kwargs = _http()
            r = http.get(url, headers={"User-Agent": _UA}, timeout=20,
                         allow_redirects=True, **kwargs)
            m = re.search(r"id_([A-Za-z0-9]+={0,2})", r.url)
            if m:
                return m.group(1)
        except Exception:  # noqa: BLE001
            pass
    return ""


def _ups(vid: str) -> dict:
    """调 ups.get.json，返回 data；失败打印接口错误并返回 None。"""
    cookie, cna = _load_cookie()
    if not cna:
        cna = _anonymous_cna()
    params = {
        "vid": vid, "ccode": _CCODE, "client_ip": "192.168.1.1",
        "utid": cna, "client_ts": time.time(),
    }
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    if cookie:
        headers["Cookie"] = cookie
    http, kwargs = _http()
    r = http.get(_API, params=params, headers=headers, timeout=30, **kwargs)
    data = r.json().get("data") or {}
    error = data.get("error") or {}
    if error:
        code, note = error.get("code"), (error.get("note") or "").strip()
        if code == -3007:
            print("  [提示] 该视频需要登录，请导出 www.youku.com_cookies.txt（见 README）。")
        elif code == -6004:
            print("  [提示] 优酷提示账号被风控（-6004），换个时间或账号再试。")
        else:
            print(f"  [提示] 优酷接口返回: {code} {note}")
        return None
    return data


def fetch_video(url: str) -> VideoItem | None:
    """解析视频信息和可选画质列表（formats 里带 stream_index，选定后调 resolve_stream）。"""
    vid = extract_vid(url)
    if not vid:
        print("  [提示] 没能从链接里解析出优酷视频 ID。")
        return None
    data = _ups(vid)
    if not data:
        return None

    # stream_type 是清晰度档位（mp4sd/mp4hd/mp4hd2v2…），同档位有国语/粤语/台配
    # 等多音轨，分辨率差几个像素；按档位去重，优先国语
    lang_pri = {"guoyu": 0, "default": 1}
    best_by_type: dict[str, tuple] = {}
    for i, s in enumerate(data.get("stream") or []):
        w, h = s.get("width") or 0, s.get("height") or 0
        if not (w and h and s.get("segs")):
            continue
        pri = lang_pri.get(s.get("audio_lang") or "", 2)
        key = s.get("stream_type") or f"{w}x{h}"
        if key not in best_by_type or pri < best_by_type[key][0]:
            best_by_type[key] = (pri, i, s)

    formats = []
    for _pri, i, s in sorted(best_by_type.values(),
                             key=lambda t: -((t[2].get("width") or 0)
                                             * (t[2].get("height") or 0))):
        w, h = s.get("width") or 0, s.get("height") or 0
        size = s.get("size") or 0
        lang = _LANG_NAMES.get(s.get("audio_lang") or "", s.get("audio_lang") or "")
        label = f"{w}x{h}"
        if lang:
            label += f" {lang}"
        if size:
            label += f" ~{size / 1048576:.0f}MB"
        formats.append({"label": label, "stream_index": i})

    return VideoItem(
        platform="youku",
        video_id=vid,
        title=(data.get("video") or {}).get("title") or "",
        url=f"https://v.youku.com/v_show/id_{vid}.html",
        formats=formats,
    )


def resolve_stream(vid: str, stream_index: int) -> list[str]:
    """按选定的流序号取分段 mp4 直链列表（segs[].cdn_url）。

    不用 m3u8_url：它是个通用主播放列表，里面的档位和所选清晰度可能对不上；
    segs 才是该清晰度自己的分段地址。分段下载后由 downloader 用 ffmpeg 拼接。
    """
    data = _ups(vid)
    if not data:
        return []
    streams = data.get("stream") or []
    if not (0 <= stream_index < len(streams)):
        return []
    segs = streams[stream_index].get("segs") or []
    return [s["cdn_url"] for s in segs if s.get("cdn_url")]
