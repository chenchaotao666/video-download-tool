"""视频元数据结构。"""

from dataclasses import dataclass, field


@dataclass
class VideoItem:
    platform: str          # "douyin" / "bilibili"
    video_id: str          # 平台内唯一 ID（yt-dlp 下载路径用不到，可留空）
    title: str
    url: str
    direct_url: str = ""   # CDN 直链（抖音浏览器通道产出），设置后下载不走 yt-dlp
    images: list[str] = field(default_factory=list)  # 抖音图文帖的图片直链，非空则按图片集下载
    formats: list[dict] = field(default_factory=list)  # 可选画质列表 [{label, ...}]，供用户挑选
    segment_urls: list[str] = field(default_factory=list)  # 分段直链（腾讯长视频），多段时下载后 ffmpeg 拼接
    is_preview: bool = False  # VIP/付费试看标记（腾讯）：True 时走浏览器通道
