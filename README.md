# 抖音 / B 站 / 腾讯 / 优酷 / 爱奇艺 / YouTube 视频下载工具

给一个视频链接，下载到 `downloads/<平台>/`。支持抖音（douyin.com / v.douyin.com）、
B 站（bilibili.com / b23.tv）、腾讯视频（v.qq.com）、优酷（youku.com）、
爱奇艺（iqiyi.com）和 YouTube（youtube.com / youtu.be）。

> 仅用于个人观看/技术研究。视频版权归原作者，请勿二次分发或商用。

## 免安装版（exe）

`dist/video-downloader.exe` 是打包好的单文件版本，双击打开 → 粘贴链接 → 回车，
**列出该视频的可选画质，输入编号选择（直接回车默认第 1 项）** → 下载。
可连续粘贴多条，直接回车退出。视频下载到 exe 旁边的 `downloads/` 目录。

画质说明：默认第 1 项是该视频 Windows 能直接播放的最高画质（H.264 编码）。
标了「H.265（慎选）」的更高分辨率（如抖音的 2K/4K）Windows 自带播放器没有解码器，
下载后会只有声音没有画面，只有当你用 VLC/PotPlayer 或做后期时再选。

- **cookie**：把扩展导出的 `www.douyin.com_cookies.txt`（可选 `www.bilibili.com_cookies.txt`）
  原样放到 exe 旁边即自动启用，**不用改文件名**
- **换电脑后**：抖音功能需要浏览器，先跑一次 `video-downloader.exe --install-browser`
  （约 150MB，只装一次）；B 站功能开箱即用（ffmpeg 已内嵌）

## 从源码运行

### 安装

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # 抖音浏览器通道需要
# 国内下载慢可用镜像：
# $env:PLAYWRIGHT_DOWNLOAD_HOST="https://cdn.npmmirror.com/binaries/playwright"; .venv\Scripts\python -m playwright install chromium
```

可选：**ffmpeg** —— B 站音视频是分轨的，必须靠 ffmpeg 合并，没有它 B 站基本下不了。
项目 `bin/` 目录已自带一份 Windows 静态编译版（ffmpeg + ffprobe），系统装了则优先用系统的。
如果 `bin/` 里缺失，重新下载：

```powershell
curl -L "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz" -o bin\ffmpeg.exe.gz
curl -L "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffprobe-win32-x64.gz" -o bin\ffprobe.exe.gz
# 解压 .gz 得到 ffmpeg.exe / ffprobe.exe（可用 7zip 或 python -m gzip）
```

### 用法

```powershell
# 方式一（推荐）：不带参数运行，提示后粘贴链接——不用加引号，不会踩 PowerShell 的 & 坑
.venv\Scripts\python main.py

# 方式二：命令行直接传链接，链接里带 & 参数时必须加双引号
.venv\Scripts\python main.py "https://www.douyin.com/video/xxx"
.venv\Scripts\python main.py "https://v.douyin.com/xxx/"
.venv\Scripts\python main.py "https://www.bilibili.com/video/BVxxx"

# 从抖音「我的收藏」等页面复制的主页弹窗链接也可以直接用：
.venv\Scripts\python main.py "https://www.douyin.com/user/self?modal_id=xxx&showTab=favorite_collection"
```

抖音走 `video_agent/douyin_browser.py` 的 Playwright 浏览器通道（不经过 yt-dlp，
因为抖音 Web API 强制 `a_bogus` 动态签名，直连频繁被 403/验证码风控）：
真实浏览器打开页面、拦截页面自己发出的 detail 接口响应拿到无水印 CDN 直链，
再直连下载。被验证码拦截时会自动弹出浏览器窗口，手动过一下验证即可继续。

图文帖（图片轮播）不是视频——它的 play_addr 是黑场占位，直接下会拿到黑屏 mp4。
工具会识别图文帖，把全部图片按 `01.webp、02.webp……` 保存到同名文件夹。

腾讯视频走 `video_agent/tencent.py`：网页播放器是 fMP4 分段流（JS 内核实时拼接，
不好直接下），改为调 getvinfo 接口拿 vkey 签名的整段 mp4 直链；长视频分段则
逐段下载后用 ffmpeg 无损拼接。支持剧集主页链接（自动取第一集）。
**VIP/付费内容**匿名只能试看（CDN 会"正常"返回截断的流，工具会用 Range 探测出来
并提示）。配好登录 cookie（`v.qq.com_cookies.txt`）后自动走 h5vv6 getvinfo
接口（ckey 签名）：一次请求拿到全部清晰度的 HLS(m3u8) 地址，选定画质后
ffmpeg 直接拉流。VIP 清晰度可到 1080P/4K（以账号权限为准）。

### 重新打包（zip 免安装版）

```powershell
python build.py
```

一条命令产出绿色版 `dist\`（`video-downloader.exe` + `browsers\` 浏览器），
整个目录打 zip 分发即可，用户解压双击即用，无需任何安装。

`build.py` 自动完成：安装依赖 → 准备 `bin\ffmpeg.exe`（PATH 没有就从
gyan.dev 下载）→ PyInstaller onefile 打包（内嵌 ffmpeg 和 Playwright 驱动）
→ 把 Chromium 装进 `dist\browsers\`（优先复用本机 `%LOCALAPPDATA%\ms-playwright`
缓存，缺的才下载）→ 清理 `dist\` 里测试产生的 downloads（cookie 文件保留不动）。

注意：
- 需要 Windows + Python 3.10+，首次构建联网下载约 700MB（ffmpeg + Chromium）
- 可重复运行，已下载的 ffmpeg / 浏览器自动复用
- 打包前会自动结束正在运行的 video-downloader.exe（旧实例会锁住 exe 导致构建失败）
- 打 zip 分发前检查 dist 里的 `*_cookies.txt`（含登录凭据），构建时脚本会提醒

优酷走 `video_agent/youku.py`：调 ups.get.json 接口（ccode=0530）拿分段 mp4 直链，
逐段下载后 ffmpeg 无损拼接。yt-dlp 自带的优酷提取器用的 ccode=0564 已被服务端
判定需登录（-3007），其它常见 ccode 会触发账号风控（-6004），所以没用它。
热门内容大多要求登录，会员内容需要会员账号 + cookie。

爱奇艺走 `video_agent/iqiyi.py`：桌面播放页是 JS 空壳拿不到 tvid，改抓移动端页面
（m.iqiyi.com）内嵌的 loadInfo 拿 tvid/vid，再调 tmts 接口（md5 签名，算法同
yt-dlp 已失效的提取器——页面提取死了但接口签名还有效）拿各清晰度 m3u8，
ffmpeg 直下 HLS。VIP 内容匿名只能试看（m3u8 带 prv 试看参数，工具会识别并提示），
登录 cookie（`www.iqiyi.com_cookies.txt` 等）带上后返回完整播放列表。

YouTube 走 yt-dlp（`video_agent/downloader.py`）。两个前置条件：
- **代理**：YouTube 被墙，需要 Clash 等代理开着（yt-dlp 自动读 http_proxy 环境变量）
- **JS 运行时**：YouTube 的播放地址是签名加密的，解密要 node 或 deno
  （`winget install OpenJS.NodeJS` 装一个即可；源码运行检测到即可用，
  exe 版需要目标机器也装有 node/deno）
触发「Sign in to confirm you're not a bot」人机验证时，导出
`www.youtube.com_cookies.txt` 放到旁边即可。

cookie 文件名按导出时所在页面域名：`www.youku.com_cookies.txt`、`youku.com_cookies.txt`、
`v.youku.com_cookies.txt` 都认。

## cookie 导出（应对风控）

- **B 站**：无 cookie 下载大概率被风控拒绝（HTTP 412）。
- **抖音**：可能被验证码挡或只能拿到低清晰度。
- **腾讯视频**：无 cookie 时 VIP/付费内容只能试看一小段；免费内容不受影响。

操作步骤（一次性，两个站点通用）：

1. 浏览器装扩展 "Get cookies.txt LOCALLY"（Firefox 装 "cookies.txt"），
   Chrome/Edge 直装链接：
   https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
2. 登录 bilibili.com / douyin.com / v.qq.com / youku.com / iqiyi.com，停留在对应页面上
   点扩展 → Export，得到 `www.bilibili.com_cookies.txt` / `www.douyin.com_cookies.txt` /
   `v.qq.com_cookies.txt` / `v.youku.com_cookies.txt` / `www.iqiyi.com_cookies.txt`；
3. 文件名**保持原样**，复制到项目根目录（或 exe 旁边）即自动启用。

cookie 大约每月过期一次，下载重新被拦时重新导出即可。该文件等同登录凭证，
已加入 .gitignore，不要提交或分享。

## 项目结构

```
main.py                     # 入口：python main.py <视频URL>
config.py                   # 下载目录、cookie 文件路径、下载画质
video_agent/
  models.py                 # VideoItem：视频元数据结构
  downloader.py             # yt-dlp 下载封装；有 CDN 直链时直连下载
  douyin_browser.py         # 抖音 Playwright 浏览器兜底（绕 a_bogus 签名）
downloads/<platform>/       # 下载产物（已 gitignore）
```
