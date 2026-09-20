"""腾讯视频抓取：调 getvinfo 接口拿带 vkey 签名的 CDN 直链。

网页播放器用的是 fMP4 分段流（浏览器里靠 JS 内核实时拼接，不好直接下载），
但老接口 vv.video.qq.com/getinfo 仍返回完整 mp4 的签名直链，绕开网页播放器。

VIP/付费内容走 h5vv6.video.qq.com/getvinfo（ckey 签名，算法同 yt-dlp 的
tencent 提取器）：网页播放器的 vinfo_proxy 响应已加密（enc=1），浏览器拦截
拿不到明文；h5vv6 接口带 ckey + 登录 cookie 直接返回明文 vinfo，
含各清晰度列表和 HLS(m3u8) 地址，不再需要浏览器通道。

登录 cookie（v.qq.com_cookies.txt 或 www.qq.com_cookies.txt，浏览器扩展导出）
存在时自动带上，可解锁更高清晰度和 VIP 内容（以账号权限为准）。
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from urllib.parse import parse_qs, urlparse

import config
from .models import VideoItem

_API = "https://vv.video.qq.com/getinfo"
_REFERER = "https://v.qq.com/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 扩展导出时的文件名取决于导出时所在页面，两个名字都认
_COOKIE_FILES = ("v.qq.com_cookies.txt", "www.qq.com_cookies.txt")

# v.qq.com 站点的认证 cookie 带 v_ 前缀（v_vuserid/v_vusession…），
# 但 getinfo/getvinfo 接口只认标准名（vuserid/vusession…），不认会返回
# login=0 匿名态，VIP 清晰度出不来。导出文件里只有前缀版，这里补一份标准名。
_AUTH_COOKIE_RENAME = {
    "v_vuserid": "vuserid",
    "v_vusession": "vusession",
    "v_t_openid": "openid",
    "v_t_access_token": "access_token",
    "v_t_appid": "appid",
}


def _http():
    """优先 curl_cffi（模拟 Chrome 的 TLS 指纹），没有则用 requests。"""
    try:
        from curl_cffi import requests as http
        return http, {"impersonate": "chrome"}
    except ImportError:
        import requests as http
        return http, {}


def _cookie_header() -> str:
    """Netscape cookie 文件 -> Cookie 请求头。"""
    for name in _COOKIE_FILES:
        path = config.BASE_DIR / name
        if not (path.exists() and path.stat().st_size > 0):
            continue
        pairs = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.lstrip("#HttpOnly_")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                pairs[parts[5]] = parts[6]
        # 补上接口认的标准名认证 cookie（见 _AUTH_COOKIE_RENAME）
        for old, new in _AUTH_COOKIE_RENAME.items():
            if old in pairs:
                pairs.setdefault(new, pairs[old])
        if pairs:
            # v_qq_com_session_lapse_time 是个毫秒时间戳，服务端拿它判断登录
            # 会话是否失效。真实浏览器里站点会持续续期，但导出文件里是死值，
            # 导出后约 1.5 小时就过期，之后 VIP 接口报 4008
            # "missing session lapse time"。该值本身不是密钥，续期到 2 小时后
            # 即可通过校验（实测 vinfo 由 4008 恢复为 is_vip:true）。
            pairs["v_qq_com_session_lapse_time"] = str(
                int((time.time() + 7200) * 1000))
            return "; ".join(f"{k}={v}" for k, v in pairs.items())
    return ""


def extract_vid(url: str) -> str:
    """从各种腾讯视频链接里提取 vid（视频 ID）。

    支持 /x/page/<vid>.html、/x/cover/<cid>/<vid>.html、/x/cover/<cid>.html
    （剧集主页，取第一集）、m.v.qq.com/play.html?vid=、iframe player.html?vid=。
    """
    m = re.search(r"/x/cover/[a-z0-9]+/([a-z0-9]+)\.html", url)
    if m:
        return m.group(1)
    m = re.search(r"/x/page/([a-z0-9]+)\.html", url)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url).query).get("vid", [""])[0]
    if q:
        return q
    m = re.search(r"/x/cover/([a-z0-9]+)\.html", url)
    if m:
        # 剧集主页：页面 pinia 数据里带第一集的 vid
        cid = m.group(1)
        http, kwargs = _http()
        r = http.get(f"https://v.qq.com/x/cover/{cid}.html",
                     headers={"User-Agent": _UA, "Referer": _REFERER},
                     timeout=30, **kwargs)
        for cand in re.findall(r'"vid":"([a-z0-9]{10,20})"', r.text):
            if cand != cid:
                return cand
    return ""


def _getvinfo(vid: str, defn: str = "shd", idx: int | None = None) -> dict:
    """调 getvinfo 接口。defn 是清晰度标识（hd/shd/fhd…），idx 是分段序号。"""
    params = (f"vids={vid}&platform=11001&charge=0&otype=json&defn={defn}"
              f"&ehost=https%3A%2F%2Fv.qq.com")
    if idx is not None:
        params += f"&idx={idx}"
    http, kwargs = _http()
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    r = http.get(f"{_API}?{params}", headers=headers, timeout=30, **kwargs)
    t = r.text
    if t.startswith("QZOutputJson="):
        t = t[len("QZOutputJson="):].rstrip(";")
    return json.loads(t)


def _vi(data: dict) -> dict | None:
    """取 vl.vi[0]，失败时打印接口给的错误信息（审核中/VIP 限制等）。"""
    vi_list = (data.get("vl") or {}).get("vi") or []
    if not vi_list:
        msg = data.get("msg") or json.dumps(data, ensure_ascii=False)[:200]
        print(f"  [提示] 腾讯视频接口返回: {msg}")
        return None
    return vi_list[0]


def fetch_video(url: str) -> VideoItem | None:
    """解析视频信息和可选画质列表（formats 里带 defn，选定后调 resolve_stream）。

    顺带判定 VIP 试看：对未分段的视频用 Range 探测实际可下载字节数，
    和接口给的全片大小 fs 对比，结果存进 item.is_preview。
    """
    vid = extract_vid(url)
    if not vid:
        print("  [提示] 没能从链接里解析出腾讯视频 ID。")
        return None
    data = _getvinfo(vid)
    vi = _vi(data)
    if not vi:
        return None

    formats = []
    for f in ((data.get("fl") or {}).get("fi") or []):
        w, h = f.get("width") or 0, f.get("height") or 0
        if not (w and h):
            continue
        cname = (f.get("cname") or "").replace(";", " ")
        formats.append({"label": f"{cname} {w}x{h}", "defn": f.get("name"),
                        "height": h})
    formats.sort(key=lambda f: -f["height"])

    # 试看判定（未分段的才探测；分段视频的第一个分段远小于全片，会误判）
    is_preview = False
    ci = (vi.get("cl") or {}).get("ci") or []
    ui = (vi.get("ul") or {}).get("ui") or []
    fs = vi.get("fs") or 0
    if not ci and ui and vi.get("fn") and vi.get("fvkey") and fs:
        probe_url = f"{ui[0]['url']}{vi['fn']}?vkey={vi['fvkey']}"
        is_preview = not _is_full_stream(probe_url, fs)

    return VideoItem(
        platform="tencent",
        video_id=vid,
        title=vi.get("ti") or "",
        url=f"https://v.qq.com/x/page/{vid}.html",
        formats=formats,
        is_preview=is_preview,
    )


def resolve_stream(vid: str, defn: str) -> list[str]:
    """按选定清晰度（defn）取签名直链。长视频会分段，返回分段直链列表。

    VIP 试看判定已在 fetch_video 里做过（is_preview），这里只负责取地址。
    """
    data = _getvinfo(vid, defn)
    vi = _vi(data)
    if not vi:
        return []

    segments = [c.get("idx") for c in ((vi.get("cl") or {}).get("ci") or [])
                if c.get("idx")]
    responses = [(None, data)] if not segments else [
        (idx, _getvinfo(vid, defn, idx)) for idx in segments]

    urls = []
    for idx, d in responses:
        v = vi if idx is None else _vi(d)
        if not v:
            return []
        ui = (v.get("ul") or {}).get("ui") or []
        base = ui[0].get("url", "") if ui else ""
        if not base or not v.get("fn") or not v.get("fvkey"):
            print("  [提示] 接口没返回播放地址（可能是 VIP/权限限制）。")
            return []
        urls.append(f"{base}{v['fn']}?vkey={v['fvkey']}")
    return urls


def _is_full_stream(url: str, full_size: int) -> bool:
    """Range bytes=0-0 探测 CDN 实际可下载的总量，与全片大小对比。"""
    try:
        http, kwargs = _http()
        r = http.get(url, headers={"User-Agent": _UA, "Referer": _REFERER,
                                   "Range": "bytes=0-0"}, timeout=30, **kwargs)
        content_range = r.headers.get("Content-Range", "")  # 形如 bytes 0-0/213624316
        total = int(content_range.rsplit("/", 1)[-1])
        return total >= full_size * 0.95
    except Exception:  # noqa: BLE001 - 探测失败不阻塞，交给下载环节
        return True


def _tencent_cookie_path():
    """第一个存在且非空的腾讯 cookie 文件。"""
    for name in _COOKIE_FILES:
        path = config.BASE_DIR / name
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def has_cookie() -> bool:
    return _tencent_cookie_path() is not None


# ---- VIP/付费通道：h5vv6 getvinfo（ckey 签名，算法同 yt-dlp tencent 提取器）----

_API_V2 = "https://h5vv6.video.qq.com/getvinfo"
_APP_VER = "3.5.57"
_PLATFORM_V2 = "10901"


def extract_cid(url: str) -> str:
    """从 /x/cover/<cid>/<vid>.html 链接里取剧集 ID（cid）。"""
    m = re.search(r"/x/cover/([a-z0-9]+)/[a-z0-9]+\.html", url)
    return m.group(1) if m else ""


def _get_ckey(vid: str, page_url: str, guid: str) -> str:
    """getvinfo 的 ckey 签名（AES-CBC，密钥/算法同 yt-dlp 的 tencent 提取器）。"""
    from yt_dlp.aes import aes_cbc_encrypt_bytes
    payload = (f"{vid}|{int(time.time())}|mg3c3b04ba|{_APP_VER}|{guid}|"
               f"{_PLATFORM_V2}|{page_url[:48]}|{_UA.lower()[:48]}||Mozilla|"
               "Netscape|Windows x86_64|00|")
    return aes_cbc_encrypt_bytes(
        bytes(f"|{sum(map(ord, payload))}|{payload}", "utf-8"),
        b"Ok\xda\xa3\x9e/\x8c\xb0\x7f^r-\x9e\xde\xf3\x14",
        b"\x01PJ\xf3V\xe6\x19\xcf.B\xbb\xa6\x8c?p\xf9",
        padding_mode="whitespace").hex().upper()


def _getvinfo_v2(vid: str, cid: str, page_url: str, defn: str) -> dict:
    """调 h5vv6 getvinfo：ckey 签名 + 登录 cookie，返回明文 vinfo。"""
    guid = "".join(random.choices(string.digits + string.ascii_lowercase, k=16))
    params = {
        "vid": vid, "cid": cid, "cKey": _get_ckey(vid, page_url, guid),
        "encryptVer": "8.1",
        "sphls": "2", "dtype": "3",       # 只要 HLS(m3u8) 地址，交给 ffmpeg 下
        "defn": defn, "spsrt": "2", "sphttps": "1", "otype": "json",
        "spwm": "1", "hevclv": "28", "spvideo": "4", "spsfrhdr": "100",
        "host": "v.qq.com", "referer": "v.qq.com", "ehost": page_url,
        "appVer": _APP_VER, "platform": _PLATFORM_V2, "guid": guid,
        "flowid": "".join(random.choices(string.digits + string.ascii_lowercase,
                                         k=32)),
    }
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    http, kwargs = _http()
    r = http.get(_API_V2, params=params, headers=headers, timeout=30, **kwargs)
    t = r.text
    if t.startswith("QZOutputJson="):
        t = t[len("QZOutputJson="):].rstrip(";")
    return json.loads(t)


def fetch_vip_qualities(vid: str, cid: str, page_url: str) -> list[dict]:
    """VIP 清晰度列表：[{label, defn, fs}]，按文件大小从高到低。"""
    data = _getvinfo_v2(vid, cid, page_url, "hd")
    msg = data.get("msg")
    if msg and str(data.get("code")) not in ("0", "0.0"):
        print(f"  [提示] 腾讯视频接口返回: {msg}")
    qualities = []
    for f in (data.get("fl") or {}).get("fi") or []:
        if f.get("drm"):
            continue  # DRM 流下了也播不了
        cname = (f.get("cname") or "").replace(";", " ")
        size = f.get("fs") or 0
        label = cname + (f" ~{size / 1048576:.0f}MB" if size else "")
        qualities.append({"label": label, "defn": f.get("name"), "fs": size})
    qualities.sort(key=lambda q: -q["fs"])
    return qualities


def resolve_vip_hls(vid: str, cid: str, page_url: str, defn: str) -> str:
    """按选定清晰度（defn）取 HLS(m3u8) 地址；没有 HLS 时退回 mp4 直链。"""
    data = _getvinfo_v2(vid, cid, page_url, defn)
    vi = _vi(data)
    if not vi:
        return ""
    ui_list = (vi.get("ul") or {}).get("ui") or []
    for ui in ui_list:
        url = ui.get("url") or ""
        pt = (ui.get("hls") or {}).get("pt") or ""
        if pt or ".m3u8" in url:
            return url + pt
    base = ui_list[0].get("url", "") if ui_list else ""
    if base and vi.get("fn") and vi.get("fvkey"):
        return f"{base}{vi['fn']}?vkey={vi['fvkey']}"
    print("  [提示] 接口没返回播放地址（可能是 VIP/权限限制）。")
    return ""

