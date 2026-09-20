"""腾讯视频抓取：调 getvinfo 接口拿带 vkey 签名的 CDN 直链。

网页播放器用的是 fMP4 分段流（浏览器里靠 JS 内核实时拼接，不好直接下载），
但老接口 vv.video.qq.com/getinfo 仍返回完整 mp4 的签名直链，绕开网页播放器。

登录 cookie（v.qq.com_cookies.txt 或 www.qq.com_cookies.txt，浏览器扩展导出）
存在时自动带上，可解锁更高清晰度和 VIP 内容（以账号权限为准）。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import config
from .models import VideoItem

_API = "https://vv.video.qq.com/getinfo"
_REFERER = "https://v.qq.com/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 扩展导出时的文件名取决于导出时所在页面，两个名字都认
_COOKIE_FILES = ("v.qq.com_cookies.txt", "www.qq.com_cookies.txt")


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
        pairs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.lstrip("#HttpOnly_")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                pairs.append(f"{parts[5]}={parts[6]}")
        if pairs:
            return "; ".join(pairs)
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


def capture_vip_hls(page_url: str) -> dict | None:
    """VIP/付费通道：浏览器打开播放页（带登录 cookie），拦截 proxyhttp 播放数据。

    播放器的 vinfo 请求（POST vd6.l.qq.com/proxyhttp）响应里带有完整 HLS
    播放列表（内嵌 m3u8，分段含 token），保存请求参数后可以用不同清晰度
    重放。一次请求搞定，不用遍历播放。

    返回 {"qualities": [{label, defn}], "post_url": str, "post_body": dict}，
    失败返回 None。
    """
    cookie_path = _tencent_cookie_path()
    if not cookie_path:
        print("  [提示] VIP 通道需要登录 cookie。")
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("需要 playwright") from e
    from .douyin_browser import _UA, _load_cookies

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=_UA, locale="zh-CN",
                                  viewport={"width": 1920, "height": 1080})
        ctx.add_cookies(_load_cookies(cookie_path))
        page = ctx.new_page()
        try:
            with page.expect_response(
                    lambda r: "proxyhttp" in r.url, timeout=30000) as resp_info:
                page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            req = resp_info.value.request
            vinfo = json.loads(json.loads(resp_info.value.body()
                                          .decode("utf-8", "replace"))["vinfo"])
            post_url, post_body = req.url, json.loads(req.post_data)
        except Exception as e:  # noqa: BLE001
            print(f"  [提示] 没拦截到播放数据（可能遇到验证码）: {e}")
            return None
        finally:
            browser.close()

    qualities = []
    for f in (vinfo.get("fl") or {}).get("fi") or []:
        if f.get("drm"):
            continue  # DRM 流下了也播不了
        cname = (f.get("cname") or "").replace(";", " ")
        size = f.get("fs") or 0
        label = cname
        if size:
            label += f" ~{size / 1048576:.0f}MB"
        qualities.append({"label": label, "defn": f.get("name"),
                          "fs": size})
    qualities.sort(key=lambda q: -q["fs"])
    return {"qualities": qualities, "post_url": post_url, "post_body": post_body}


def resolve_vip_hls(post_url: str, post_body: dict, defn: str) -> str:
    """用拦截到的播放请求参数重放 proxyhttp，换 defn 拿对应清晰度的 m3u8 地址。"""
    body = dict(post_body)
    vp = body.get("vinfoparam", "")
    if re.search(r"&defn=", vp):
        vp = re.sub(r"&defn=[^&]*", f"&defn={defn}", vp)
    else:
        vp += f"&defn={defn}"
    body["vinfoparam"] = vp

    http, kwargs = _http()
    r = http.post(post_url, json=body, timeout=30, **{
        **kwargs,
        "headers": {"User-Agent": _UA, "Referer": _REFERER,
                    "Cookie": _cookie_header(), "Content-Type": "application/json"},
    })
    vinfo = json.loads(r.json()["vinfo"])
    vi = _vi({"vl": vinfo.get("vl")})
    if not vi:
        return ""
    ui = (vi.get("ul") or {}).get("ui") or []
    return ui[0].get("url", "") if ui else ""

