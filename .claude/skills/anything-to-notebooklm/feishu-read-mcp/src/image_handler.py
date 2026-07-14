"""图片处理器"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse
import aiohttp
from aiohttp import ClientTimeout
import io
from PIL import Image
import json

logger = logging.getLogger(__name__)


class ImageHandler:
    """飞书文档图片处理器"""

    def __init__(self, cookies_str: Optional[str] = None):
        """
        初始化图片处理器

        Args:
            cookies_str: 可选的cookie字符串
        """
        self.image_dir = Path("/tmp/feishu_images")
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = ClientTimeout(total=30, connect=10)
        self.cookies_str = cookies_str

    async def download_images(self, image_urls: List[str]) -> Dict[str, str]:
        """
        批量下载图片

        Args:
            image_urls: 图片URL列表

        Returns:
            Dict[str, str]: {原始URL: 本地路径} 的映射
        """
        if not image_urls:
            return {}

        logger.info(f"Starting to download {len(image_urls)} images...")

        # 准备headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        # 准备cookies（如果提供）
        cookies = None
        if self.cookies_str:
            cookie_list = []
            for cookie_pair in self.cookies_str.split(';'):
                if '=' in cookie_pair:
                    key, value = cookie_pair.strip().split('=', 1)
                    cookie_list.append({
                        'name': key,
                        'value': value,
                        'domain': '.feishu.cn'
                    })
            cookies = cookie_list

        # 创建会话
        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers, cookies=cookies) as session:
            # 并发下载（限制并发数为5）
            semaphore = asyncio.Semaphore(5)

            async def download_single(url: str) -> tuple[str, str]:
                """下载单个图片"""
                async with semaphore:
                    return await self._download_image(session, url)

            # 执行并发下载
            results = await asyncio.gather(
                *[download_single(url) for url in image_urls],
                return_exceptions=True
            )

            # 过滤结果
            downloaded = {}
            for result in results:
                if isinstance(result, tuple):
                    original_url, local_path = result
                    downloaded[original_url] = local_path

        logger.info(f"Successfully downloaded {len(downloaded)} images")

        return downloaded

    async def _download_image(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
        """
        下载单个图片

        Args:
            session: aiohttp 会话
            url: 图片URL

        Returns:
            tuple: (原始URL, 本地路径)
        """
        try:
            # 生成文件名
            file_hash = hashlib.md5(url.encode()).hexdigest()
            file_ext = self._get_file_extension(url)
            file_path = self.image_dir / f"{file_hash}{file_ext}"

            # 如果文件已存在，直接返回
            if file_path.exists():
                logger.debug(f"Image already exists: {file_path}")
                return url, str(file_path)

            # 发起请求
            async with session.get(url) as response:
                # 跳过认证错误和其他客户端错误
                if response.status in [401, 403]:
                    logger.warning(f"Skipping image {url}: Authentication required (HTTP {response.status})")
                    return url, url  # 返回原始URL
                if response.status != 200:
                    logger.warning(f"Failed to download image {url}: HTTP {response.status}")
                    return url, url  # 返回原始URL

                # 读取图片数据
                content = await response.read()

                # 验证图片格式
                if not self._is_valid_image(content):
                    logger.warning(f"Invalid image format for {url}")
                    return url, url

                # 优化图片（可选）
                optimized_content = await self._optimize_image(content)

                # 保存到本地
                with open(file_path, 'wb') as f:
                    f.write(optimized_content)

                logger.debug(f"Downloaded image: {file_path}")
                return url, str(file_path)

        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading image: {url}")
            return url, url
        except Exception as e:
            logger.error(f"Error downloading image {url}: {e}")
            return url, url

    def _get_file_extension(self, url: str) -> str:
        """
        从 URL 获取文件扩展名

        Args:
            url: 图片URL

        Returns:
            str: 文件扩展名
        """
        parsed = urlparse(url)
        path = parsed.path

        # 常见的图片扩展名
        extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp']

        for ext in extensions:
            if path.lower().endswith(ext):
                return ext

        # 默认扩展名
        return '.png'

    def _is_valid_image(self, content: bytes) -> bool:
        """
        验证是否为有效的图片

        Args:
            content: 图片二进制数据

        Returns:
            bool: 是否为有效图片
        """
        # 检查文件大小（太小或太大都认为无效）
        if len(content) < 100 or len(content) > 10 * 1024 * 1024:
            return False

        # 检查文件头
        image_signatures = {
            b'\x89PNG': 'png',
            b'\xff\xd8\xff': 'jpeg',
            b'GIF8': 'gif',
            b'RIFF': 'webp',  # WebP 文件头
            b'BM': 'bmp'
        }

        for signature in image_signatures:
            if content.startswith(signature):
                return True

        return False

    async def _optimize_image(self, content: bytes) -> bytes:
        """
        优化图片（压缩、转换格式等）

        Args:
            content: 原始图片数据

        Returns:
            bytes: 优化后的图片数据
        """
        try:
            # 如果内容太大，进行压缩
            if len(content) > 1024 * 1024:  # 大于1MB
                image = Image.open(io.BytesIO(content))

                # 如果是 RGBA，转换为 RGB（JPEG 不支持透明度）
                if image.mode == 'RGBA':
                    # 创建白色背景
                    background = Image.new('RGB', image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1])
                    image = background
                elif image.mode != 'RGB':
                    image = image.convert('RGB')

                # 限制最大尺寸
                max_size = (1920, 1080)
                image.thumbnail(max_size, Image.Resampling.LANCZOS)

                # 保存为 JPEG（质量85）
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=85, optimize=True)

                return output.getvalue()

            return content

        except Exception as e:
            logger.warning(f"Failed to optimize image: {e}")
            return content

    async def download_single_image(self, url: str, session: aiohttp.ClientSession) -> str:
        """
        下载单个图片并返回本地路径

        Args:
            url: 图片URL
            session: aiohttp 会话

        Returns:
            str: 本地文件路径
        """
        _, local_path = await self._download_image(session, url)
        return local_path

    def get_image_info(self, image_path: str) -> Dict:
        """
        获取图片信息

        Args:
            image_path: 图片路径

        Returns:
            Dict: 图片信息（尺寸、大小等）
        """
        try:
            path = Path(image_path)
            if not path.exists():
                return {}

            image = Image.open(path)
            return {
                "path": str(path),
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "format": image.format,
                "size": path.stat().st_size
            }
        except Exception as e:
            logger.error(f"Error getting image info for {image_path}: {e}")
            return {}

    async def cleanup(self):
        """清理临时图片文件"""
        try:
            # 删除7天前的临时图片
            import time
            now = time.time()
            seven_days_ago = now - 7 * 24 * 60 * 60

            for file_path in self.image_dir.glob('*'):
                if file_path.is_file():
                    mtime = file_path.stat().st_mtime
                    if mtime < seven_days_ago:
                        file_path.unlink()
                        logger.debug(f"Cleaned up old image: {file_path}")
        except Exception as e:
            logger.error(f"Error cleaning up images: {e}")
