"""配置：下载目录、cookie 文件、下载画质。"""

import sys
from pathlib import Path

# 打包成 exe（PyInstaller）后 __file__ 指向临时解压目录，
# 下载目录、cookie 等用户文件要相对 exe 自身位置定位
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# 下载目录（按平台分子目录）
DOWNLOAD_DIR = BASE_DIR / "downloads"

# 登录 cookie（Netscape 格式，浏览器扩展 "Get cookies.txt LOCALLY" 导出，
# 导出默认就是这个文件名，放到 exe/项目旁边即可，方法见 README）。
# 文件存在时自动启用；没有它 B 站下载会被风控挡（HTTP 412），
# 抖音下载可能被验证码/风控挡或只能拿到低清晰度。
BILIBILI_COOKIE_FILE = BASE_DIR / "www.bilibili.com_cookies.txt"
DOUYIN_COOKIE_FILE = BASE_DIR / "www.douyin.com_cookies.txt"

# yt-dlp 下载格式：优先 mp4 容器 + H.264(avc1) 视频轨，不限制分辨率上限
# （能拿到几K取决于视频本身和账号权限：B 站 4K 需登录 cookie，部分要大会员）。
# 优先 avc1 是因为 AV1/HEVC 编码 Windows 自带播放器没有解码器，
# 会"只有声音没有画面"；实在没有 avc1 才退回任意编码。
YTDLP_FORMAT = (
    "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "bv*+ba/b"
)
