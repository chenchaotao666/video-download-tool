"""用 yt-dlp 的 Python API 下载视频；拿到 CDN 直链时直接下载。"""

import re
import shutil
import time
from pathlib import Path

import config
from .models import VideoItem

# 各平台下载时需要带的 Referer，缺了会被风控/防盗链拦
_REFERERS = {
    "bilibili": "https://www.bilibili.com/",
    "douyin": "https://www.douyin.com/",
    "iqiyi": "https://www.iqiyi.com/",
    "tencent": "https://v.qq.com/",
    "youku": "https://v.youku.com/",
    "youtube": "https://www.youtube.com/",
}


def _ffmpeg_location() -> str | None:
    """优先用系统 PATH 里的 ffmpeg；其次项目/exe 旁的 bin/；最后 exe 内嵌的 bin/。

    返回 None 表示让 yt-dlp 自己走 PATH 找；"" 表示完全没有 ffmpeg。
    B 站音视频分轨，没 ffmpeg 合并不了，基本等于下不了 B 站，
    所以项目 bin/ 里自带了一份（打包 exe 时会内嵌进去）。
    """
    import sys

    if shutil.which("ffmpeg"):
        return None
    local = config.BASE_DIR / "bin" / "ffmpeg.exe"
    if local.exists():
        return str(local.parent)
    # PyInstaller 打包时通过 --add-data 内嵌的 bin/
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "bin" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled.parent)
    return ""


def _pick_format(ffmpeg_location: str | None) -> str:
    # 音视频分轨合并需要 ffmpeg；没装就退回单文件格式（清晰度略低但能用）
    if ffmpeg_location != "":
        return config.YTDLP_FORMAT
    print("  [提示] 未检测到 ffmpeg，使用单文件格式。安装 ffmpeg 可获得更高画质。")
    return "b"


def _cookie_file(path) -> str | None:
    """cookie 文件存在且非空才返回路径；空文件（导出失败的占位）按没有处理。"""
    if path.exists() and path.stat().st_size > 0:
        return str(path)
    if path.exists():
        print(f"  [提示] {path.name} 是空文件（cookie 没导出成功），按未配置处理。")
    return None


# 各平台认的 cookie 文件名（扩展导出时的默认名，按导出时所在页面域名命名）
_PLATFORM_COOKIES = {
    "bilibili": ["www.bilibili.com_cookies.txt"],
    "douyin": ["www.douyin.com_cookies.txt"],
    "youku": ["www.youku.com_cookies.txt", "youku.com_cookies.txt",
              "v.youku.com_cookies.txt"],
}


def _platform_cookie(platform: str) -> str | None:
    """第一个存在且非空的 cookie 文件路径。"""
    for name in _PLATFORM_COOKIES.get(platform, []):
        cookie = _cookie_file(config.BASE_DIR / name)
        if cookie:
            return cookie
    return None


def download(item: VideoItem, format_spec: str | None = None) -> bool:
    """下载单个视频/图文帖，成功返回 True。format_spec 为用户选定的 yt-dlp 格式。"""
    if item.images:
        return _download_images(item)
    if item.segment_urls:
        return _download_segments(item)
    if item.direct_url:
        if ".m3u8" in item.direct_url:
            return _download_hls(item)
        return _download_direct(item)
    return _download_ytdlp(item, format_spec)


def _download_hls(item: VideoItem) -> bool:
    """HLS（m3u8）下载：ffmpeg 原生处理分段、重试和拼接。"""
    import os
    import subprocess

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        print("  [错误] HLS 下载需要 ffmpeg，但未找到。")
        return False
    outdir = config.DOWNLOAD_DIR / item.platform
    outdir.mkdir(parents=True, exist_ok=True)
    final = outdir / f"{_safe_name(item)} [{item.video_id}].mp4"
    print(f"  开始下载 HLS: {final.name}", flush=True)

    # 代理策略：CDN 会按出口 IP 限速（机房 IP 常被限到 KB 级），而"走代理"
    # 和"直连"谁快取决于平台和用户网络，没法预先知道。所以备两种环境：
    # env_with_proxy（尊重 http_proxy 等环境变量）和 env_direct（剥离代理），
    # 每次重试换一条路，配合下面的停滞看门狗，慢路 30 秒内就会被换掉。
    env_with_proxy = dict(os.environ)
    env_direct = dict(os.environ)
    has_proxy = False
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        if env_direct.pop(k, None):
            has_proxy = True
    # 有代理环境时交替尝试 [代理, 直连, 代理]；没有代理就三次都直连
    envs = [env_with_proxy, env_direct, env_with_proxy] if has_proxy \
        else [env_direct] * 3

    for attempt, env in enumerate(envs, 1):
        # HLS 总时长未知，进度只能显示已落盘的文件体积：后台跑 ffmpeg，
        # 主线程轮询目标文件大小原地刷新（1 秒一次，避免刷屏）。
        # ffmpeg 要先读完 m3u8 播放列表、拿到第一个分片才创建输出文件，
        # 文件出现前也刷一行状态，避免慢网络下界面像卡住一样没有任何提示。
        # 用 -v error 而非 warning：stderr 走管道，进程退出前不读，
        # warning 量大时可能填满管道缓冲区把 ffmpeg 卡死
        # 爱奇艺的分片是 .265ts（H.265 TS），不在 ffmpeg HLS 扩展名白名单里，
        # 不加 _hls_ext_args 会直接拒绝下载（"not in allowed_segment_extensions"）
        proc = subprocess.Popen(
            [ffmpeg, "-v", "error", "-y",
             *_hls_ext_args(ffmpeg),
             "-headers", f"Referer: {_REFERERS.get(item.platform, '')}\r\n",
             "-i", item.direct_url, "-c", "copy", str(final)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=env)
        last_print = 0.0
        # 停滞看门狗：CDN 把连接限速到几个 KB 后挂起时，ffmpeg 不会自己退出，
        # 界面会永远停在"正在连接服务器"。输出文件 30 秒不长大就杀掉换下一条路。
        last_size, last_growth = -1, time.monotonic()
        stalled = False
        while proc.poll() is None:
            time.sleep(0.5)
            now = time.monotonic()
            size = final.stat().st_size if final.exists() else 0
            if size != last_size:
                last_size, last_growth = size, now
            elif now - last_growth > 30:
                proc.kill()
                stalled = True
                break
            if now - last_print < 1:
                continue
            last_print = now
            if final.exists():
                print(f"\r  下载中 {final.stat().st_size / 1048576:.1f} MB",
                      end="", flush=True)
            else:
                print("\r  下载中（正在连接服务器）……", end="", flush=True)
        stderr = proc.stderr.read()
        if final.exists():
            # 进度行是 \r 原地刷新的，结束时补换行再打印结果
            print(f"\r  下载中 {final.stat().st_size / 1048576:.1f} MB")
        if proc.returncode == 0:
            print(f"  [HLS 下载完成] {final.name}")
            return True
        if stalled:
            via = "代理" if env is env_with_proxy else "直连"
            print(f"  [连接停滞，重试 {attempt}/3] 走{via}时服务器 30 秒没有传输数据，"
                  "换路重试。")
        else:
            print(f"  [HLS 下载中断，重试 {attempt}/3] {stderr.strip()[-200:]}")
    final.unlink(missing_ok=True)
    print(f"  [下载失败] {item.title}")
    print("  [提示] 直连和代理都拿不到数据。若你的宽带出口是机房 IP（挂热点/"
          "随身 WiFi 常见），视频站 CDN 会限速，换家庭宽带或手机流量直连再试。")
    return False


def _ffmpeg_exe() -> str | None:
    """返回 ffmpeg 可执行文件路径；找不到返回 None。"""
    loc = _ffmpeg_location()
    if loc is None:
        return shutil.which("ffmpeg")
    if loc == "":
        return None
    exe = Path(loc) / "ffmpeg.exe"
    return str(exe) if exe.exists() else None


_hls_ext_args_cache: list[str] | None = None


def _hls_ext_args(ffmpeg: str) -> list[str]:
    """放开 HLS 分片扩展名白名单（爱奇艺分片是 .265ts，不在默认名单里）。

    ffmpeg 6.1+ 的开关是 -extension_picky 0；更老的版本不认识这个选项
    （传了会直接报错退出），用 -allowed_extensions ALL 代替。
    """
    global _hls_ext_args_cache
    if _hls_ext_args_cache is not None:
        return _hls_ext_args_cache
    import subprocess
    args = ["-extension_picky", "0"]
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"ffmpeg version (\d+)\.(\d+)", out)
        if m and (int(m.group(1)), int(m.group(2))) < (6, 1):
            args = ["-allowed_extensions", "ALL"]
    except Exception:  # noqa: BLE001 - 探测失败按新版本处理（项目自带 9.x）
        pass
    _hls_ext_args_cache = args
    return args


def _download_segments(item: VideoItem) -> bool:
    """分段视频（腾讯长视频）：逐段下载后用 ffmpeg 无损拼接成单个 mp4。"""
    import subprocess
    import tempfile

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        print("  [错误] 分段视频需要 ffmpeg 拼接，但未找到 ffmpeg。")
        return False

    outdir = config.DOWNLOAD_DIR / item.platform
    outdir.mkdir(parents=True, exist_ok=True)
    final = outdir / f"{_safe_name(item)} [{item.video_id}].mp4"

    http, kwargs = _http_client()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": _REFERERS.get(item.platform, ""),
    }
    total = len(item.segment_urls)
    with tempfile.TemporaryDirectory(dir=outdir) as td:
        parts = []
        try:
            for i, url in enumerate(item.segment_urls, 1):
                part = Path(td) / f"{i:04d}.mp4"
                print(f"  下载分段 {i}/{total} ……")
                if not _fetch_to_file(http, kwargs, url, headers, part):
                    raise RuntimeError(f"分段 {i} 下载不完整")
                parts.append(part)
            # ffmpeg concat 无损拼接（不转码，秒完成）
            listfile = Path(td) / "list.txt"
            listfile.write_text(
                "".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
            proc = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listfile), "-c", "copy", str(final)],
                capture_output=True, text=True, cwd=td)
            if proc.returncode != 0:
                print(f"  [拼接失败] {proc.stderr.strip()[-400:]}")
                final.unlink(missing_ok=True)
                return False
        except Exception as e:  # noqa: BLE001 - 失败清掉半成品
            print(f"  [下载失败] {item.title}: {e}")
            final.unlink(missing_ok=True)
            return False
    print(f"  [分段拼接完成] {final.name}")
    return True


def probe_ytdlp(url: str, platform: str = "bilibili") -> list[dict]:
    """列出视频的可选画质（不下载），返回 [{label, format}]，清晰度从高到低。

    分轨平台（B 站）：每种分辨率只保留一条（优先 avc1 编码——AV1/HEVC
    Windows 自带播放器没解码器），format 是精确的 yt-dlp 格式表达式
    （视频轨 ID + 最佳音轨 ID）。
    单文件平台（优酷）：format 直接用格式 ID。
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": _REFERERS.get(platform, ""),
        },
    }
    cookie = _platform_cookie(platform)
    if cookie:
        opts["cookiefile"] = cookie

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats") or []
    # 最佳音轨：mp4a 里码率最高的（分轨平台才有独立音轨）
    audios = [f for f in formats
              if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audios.sort(key=lambda f: f.get("abr") or 0, reverse=True)
    audio_id = audios[0]["format_id"] if audios else None

    # 视频轨按分辨率去重，优先 avc1；优酷的格式是音视频合一的单文件（vcodec=None）
    best_by_res: dict[tuple, tuple] = {}
    for f in formats:
        vcodec = f.get("vcodec") or ""
        acodec = f.get("acodec") or ""
        combined = not vcodec and not acodec and f.get("url")  # 优酷：编码信息为空但有直链
        if not combined:
            # 只排除纯音轨（vcodec=none）；视频轨的 acodec 本来就是 none（分轨）
            if not vcodec or vcodec == "none":
                continue
        w, h = f.get("width") or 0, f.get("height") or 0
        if not (w and h):
            continue
        pri = 0 if (combined or vcodec.startswith("avc1")) else 1
        key = (w, h)
        if key not in best_by_res or pri < best_by_res[key][0]:
            best_by_res[key] = (pri, f)

    options = []
    for (w, h), (_, f) in sorted(best_by_res.items(),
                                 key=lambda kv: -(kv[0][0] * kv[0][1])):
        size = f.get("filesize") or f.get("filesize_approx")
        size_txt = f" ~{size / 1048576:.0f}MB" if size else ""
        codec = (f.get("vcodec") or "").split(".")[0]
        label = f"{w}x{h}{' ' + codec if codec else ''}{size_txt}"
        fmt = f["format_id"] + (f"+{audio_id}" if audio_id else "")
        options.append({"label": label, "format": fmt})
    return options


def _http_client():
    """优先 curl_cffi（模拟 Chrome 的 TLS 指纹），没有则用 requests。"""
    try:
        from curl_cffi import requests as http
        return http, {"impersonate": "chrome"}
    except ImportError:
        import requests as http
        return http, {}


def _safe_name(item: VideoItem) -> str:
    """Windows 文件名禁用的字符 + 控制字符（抖音标题常带换行 \n）都换成下划线；
    末尾的空格和点也是非法的，一并去掉。"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item.title)[:80].strip().rstrip(".") \
        or item.video_id


def _download_images(item: VideoItem) -> bool:
    """抖音图文帖：play_addr 是黑场占位视频，图片要逐张保存到文件夹。"""
    folder = config.DOWNLOAD_DIR / item.platform / f"{_safe_name(item)} [{item.video_id}]"
    folder.mkdir(parents=True, exist_ok=True)

    http, kwargs = _http_client()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": _REFERERS.get(item.platform, ""),
    }
    ok = 0
    for i, url in enumerate(item.images, 1):
        ext = ".webp" if ".webp" in url else ".jpg"
        path = folder / f"{i:02d}{ext}"
        try:
            r = http.get(url, headers=headers, timeout=60, **kwargs)
            r.raise_for_status()
            path.write_bytes(r.content)
            ok += 1
        except Exception as e:  # noqa: BLE001 - 单张失败不中断整组
            print(f"  [图片 {i} 下载失败] {e}")
    print(f"  [图文帖] {ok}/{len(item.images)} 张图片已保存到 {folder.name}/")
    return ok == len(item.images)


def _progress_text(done: int, expected: int | None) -> str:
    """进度行：有总大小显示百分比，没有就只显示已下体积。"""
    if expected:
        return (f"  下载中 {done / 1048576:.1f}/{expected / 1048576:.1f} MB"
                f"（{done * 100 // expected}%）")
    return f"  下载中 {done / 1048576:.1f} MB"


def _fetch_to_file(http, kwargs, url: str, headers: dict, path: Path) -> bool:
    """带断点续传的流式下载：校验 Content-Length，不够就用 Range 接着下。

    CDN 长连接静默断开时（腾讯/B站大文件常见）流会"正常结束"但文件不完整，
    所以必须按字节数校验，不能只看有没有抛异常。
    """
    for _attempt in range(3):
        try:
            existing = path.stat().st_size if path.exists() else 0
            h = dict(headers)
            if existing:
                h["Range"] = f"bytes={existing}-"
            # (连接, 读取) 双超时：连接keep-alive但不再吐数据时读取超时兜底
            r = http.get(url, headers=h, stream=True, timeout=(15, 120), **kwargs)
            try:
                r.raise_for_status()
                if existing and r.status_code != 206:
                    existing = 0  # 服务器没接受续传，从头下载
                total = r.headers.get("Content-Length")
                expected = existing + int(total) if total else None
                done = existing
                last_print = 0.0  # 进度刷新节流：0.2 秒一次，避免刷屏
                with open(path, "ab" if existing else "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if now - last_print >= 0.2:
                            print(f"\r{_progress_text(done, expected)}",
                                  end="", flush=True)
                            last_print = now
                # 进度行是用 \r 原地刷新的，结束时补换行，防止后续输出盖在上面
                print(f"\r{_progress_text(done, expected)}")
            finally:
                r.close()
            size = path.stat().st_size
            if expected is None or size >= expected:
                return True
            print(f"  [连接中断] 已下 {size}/{expected} 字节，断点续传……")
        except Exception as e:  # noqa: BLE001 - 重试三次后统一报失败
            print(f"  [下载中断，重试] {e}")
    path.unlink(missing_ok=True)
    return False


def _download_direct(item: VideoItem) -> bool:
    """直连 CDN 下载（抖音浏览器通道的直链），不过 yt-dlp。"""
    outdir = config.DOWNLOAD_DIR / item.platform
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{_safe_name(item)} [{item.video_id}].mp4"
    print(f"  开始下载: {path.name}")
    http, kwargs = _http_client()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": _REFERERS.get(item.platform, ""),
    }
    if _fetch_to_file(http, kwargs, item.direct_url, headers, path):
        print(f"  [直链下载完成] {path.name}")
        return True
    print(f"  [下载失败] {item.title}")
    return False


def _download_ytdlp(item: VideoItem, format_spec: str | None = None) -> bool:
    import yt_dlp

    outdir = config.DOWNLOAD_DIR / item.platform
    outdir.mkdir(parents=True, exist_ok=True)

    ffmpeg_location = _ffmpeg_location()
    # 已解析出标题/ID 的（如优酷 HLS）用统一命名；否则用 yt-dlp 元数据命名
    if item.title and item.video_id:
        outtmpl = str(outdir / f"{_safe_name(item)} [{item.video_id}].%(ext)s")
    else:
        outtmpl = str(outdir / "%(title).80s [%(id)s].%(ext)s")
    opts = {
        "format": format_spec or _pick_format(ffmpeg_location),
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": False,
        # YouTube 解签名需要 JS 运行时，yt-dlp 默认只认 deno，这里启用 node
        "js_runtimes": {"deno": {}, "node": {}},
        # 抖音/B 站对非浏览器 UA 返回验证/412，统一带浏览器 UA + 对应站点 Referer
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": _REFERERS.get(item.platform, ""),
        },
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    cookie = _platform_cookie(item.platform)
    if cookie:
        opts["cookiefile"] = cookie
    elif item.platform in ("bilibili", "douyin", "youku"):
        hints = {
            "bilibili": "B 站下载大概率被风控拒绝（412）",
            "douyin": "抖音下载可能被验证码/风控挡或只能拿到低清晰度",
            "youku": "需要登录的优酷视频会解析失败（-3007 请先登录）",
        }
        print(f"  [提示] 未找到有效的 {item.platform} cookie，{hints[item.platform]}。"
              "导出方法见 README。")
    # 大文件下载网络中断是常态，yt-dlp 会从 .part 断点续传，失败自动重试
    attempts = 3
    for i in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([item.url])
            return True
        except Exception as e:  # noqa: BLE001 - 单个视频失败不中断整批任务
            if i < attempts:
                print(f"  [下载中断，自动续传重试（第 {i + 1}/{attempts} 次）] {e}")
            else:
                print(f"  [下载失败] {item.title}: {e}")
    return False
