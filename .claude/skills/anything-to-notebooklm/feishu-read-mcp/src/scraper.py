"""飞书文档抓取器"""

import asyncio
import re
import json
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from urllib.parse import urljoin, urlparse
import logging

try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
except ImportError:
    logging.error("playwright not installed. Run: pip install playwright")
    raise

try:
    from .image_handler import ImageHandler
    from .parser import FeishuParser
except ImportError:
    from image_handler import ImageHandler
    from parser import FeishuParser

logger = logging.getLogger(__name__)


class FeishuScraper:
    """飞书文档抓取器"""

    def __init__(self, cookies_str: Optional[str] = None):
        """
        初始化飞书抓取器

        Args:
            cookies_str: 可选的cookie字符串，格式: "key1=value1; key2=value2"
        """
        self.playwright = None
        self.browser = None
        self.context = None
        self.image_handler = ImageHandler()
        self.parser = FeishuParser()
        self.temp_dir = Path("/tmp/feishu_docs")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_str = cookies_str

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.cleanup()

    async def init(self):
        """初始化 Playwright"""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )

            # 准备cookies（如果提供）
            cookies = None
            if self.cookies_str:
                cookies = []
                for cookie_pair in self.cookies_str.split(';'):
                    if '=' in cookie_pair:
                        key, value = cookie_pair.strip().split('=', 1)
                        cookies.append({
                            'name': key,
                            'value': value,
                            'domain': '.feishu.cn'
                        })

            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                extra_http_headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                },
                cookies=cookies
            )

    async def cleanup(self):
        """清理资源"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def fetch_doc(self, url: str) -> dict:
        """
        获取飞书文档并转换为 Markdown

        Args:
            url: 飞书文档URL

        Returns:
            dict: 包含标题、内容、作者、图片等信息的字典
        """
        page = await self.context.new_page()

        try:
            # 1. 打开页面
            logger.info(f"Navigating to: {url}")
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)

            # 2. 等待内容加载
            logger.info("Waiting for content to load...")
            # 初始等待5秒
            await page.wait_for_timeout(5000)

            # 等待加载指示器消失
            try:
                await page.wait_for_function(
                    """() => {
                        const loaders = document.querySelectorAll('.loading, .spinner, [class*="loading"]');
                        return loaders.length === 0;
                    }""",
                    timeout=10000
                )
            except:
                logger.warning("Could not find/disable loading indicator, continuing...")

            # 3. 获取基本信息
            title = await self._extract_title(page)
            author = await self._extract_author(page)

            # 4. 等待文档内容加载
            logger.info("Waiting for document content to appear...")

            # 等待动态渲染完成 - 使用 body 文本长度作为指标
            max_wait_time = 30000  # 30秒
            check_interval = 1000  # 每秒检查
            waited_time = 0

            while waited_time < max_wait_time:
                body_text = await page.evaluate("() => document.body.innerText.trim()")
                if len(body_text) > 500:  # 如果body文本超过500字符，认为内容已加载
                    logger.info(f"Document content detected, body text length: {len(body_text)}")
                    break
                await page.wait_for_timeout(check_interval)
                waited_time += check_interval

            if waited_time >= max_wait_time:
                logger.warning("Document content not fully loaded within timeout, proceeding with available content")

            # 5. 提取内容
            content_html = await page.evaluate("""
                () => {
                    // 首先尝试直接提取 body.innerText 作为备用
                    const bodyText = document.body.innerText.trim();
                    if (bodyText.length > 500) {
                        console.log('Using body.innerText as primary source, length:', bodyText.length);

                        // 注意：不进行内容过滤，保留所有原始文本
                        // 如果需要清理噪音内容，应该在前端或后处理阶段根据实际需求处理
                        const cleanText = bodyText;

                        // 将清理后的文本转换为HTML，更好地保留段落结构
                        let paragraphs = [];

                        // 先按双换行分割
                        const parts = cleanText.split('\\n\\n');
                        for (let part of parts) {
                            part = part.trim();
                            if (part.length === 0) continue;

                            // 过滤掉太短的段落
                            if (part.length < 10) continue;

                            // 如果这个部分太长，进一步按单换行分割
                            if (part.length > 300) {
                                const subParts = part.split('\\n');
                                let currentPara = '';
                                for (let subPart of subParts) {
                                    subPart = subPart.trim();
                                    if (subPart.length === 0) {
                                        if (currentPara.length > 0) {
                                            paragraphs.push(currentPara);
                                            currentPara = '';
                                        }
                                    } else {
                                        currentPara += (currentPara ? ' ' : '') + subPart;
                                    }
                                }
                                if (currentPara.length > 0) {
                                    paragraphs.push(currentPara);
                                }
                            } else {
                                paragraphs.push(part);
                            }
                        }

                        if (paragraphs.length > 0) {
                            return paragraphs.map(p => '<p>' + p.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim() + '</p>').join('\\n');
                        }
                    }

                    // 查找文档内容容器
                    const contentSelectors = [
                        'div[contenteditable="true"]',
                        '.larkui-theme-default',
                        '.document',
                        '.feishu-docs-content',
                        '.page-container',
                        '.wiki-page',
                        'main',
                        '[data-docx-id]'
                    ];

                    for (const selector of contentSelectors) {
                        const elements = document.querySelectorAll(selector);
                        for (const element of elements) {
                            if (element.innerText.trim().length > 100) {  // 降低阈值到100字符
                                console.log('Found content with selector:', selector, 'length:', element.innerText.length);
                                return element.innerHTML;
                            }
                        }
                    }

                    // 如果都找不到，使用包含最多文本的容器
                    const allElements = document.querySelectorAll('body > *');
                    let maxText = 0;
                    let result = '';
                    for (const element of allElements) {
                        const text = element.innerText.trim();
                        if (text.length > maxText && text.length > 100) {
                            maxText = text.length;
                            result = element.innerHTML;
                        }
                    }

                    console.log('Using fallback, max text length:', maxText);
                    return result || document.body.innerHTML;
                }
            """)

            # 6. 提取图片URL
            logger.info("Extracting images...")
            image_urls = await self._extract_image_urls(page)
            logger.info(f"Found {len(image_urls)} image URLs")

            # 7. 解析内容为结构化数据
            content_blocks = self.parser.parse_html(content_html)

            # 8. 下载图片
            logger.info(f"Downloading {len(image_urls)} images...")
            local_images = await self.image_handler.download_images(image_urls)

            # 9. 生成 Markdown
            markdown_content = self.parser.generate_markdown(
                content_blocks,
                local_images
            )

            # 10. 计算字数
            word_count = len(markdown_content.replace('\n', '').replace(' ', ''))

            logger.info(f"Successfully processed document: {title}")
            logger.info(f"Word count: {word_count}, Images: {len(local_images)}")

            return {
                "success": True,
                "title": title,
                "author": author or "Unknown",
                "content": markdown_content,
                "images": local_images,
                "word_count": word_count,
                "image_count": len(local_images),
                "url": url
            }

        except Exception as e:
            logger.error(f"Error fetching document: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
        finally:
            await page.close()

    async def get_doc_info(self, url: str) -> dict:
        """
        获取文档基本信息（不下载完整内容）

        Args:
            url: 飞书文档URL

        Returns:
            dict: 基本信息
        """
        page = await self.context.new_page()

        try:
            await page.goto(url, wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(2000)

            title = await self._extract_title(page)
            author = await self._extract_author(page)

            # 简单估算字数
            word_count = await page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    return text.replace(/\\s+/g, '').length;
                }
            """)

            # 简单估算图片数
            image_count = await page.evaluate("""
                () => {
                    return document.querySelectorAll('img').length;
                }
            """)

            return {
                "success": True,
                "title": title,
                "author": author or "Unknown",
                "word_count": word_count,
                "image_count": image_count
            }

        except Exception as e:
            logger.error(f"Error getting doc info: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
        finally:
            await page.close()

    async def _extract_title(self, page: Page) -> str:
        """提取文档标题"""
        try:
            title = await page.title()
            if title:
                return title.strip()
        except:
            pass

        try:
            title = await page.evaluate("""
                () => {
                    // 尝试多种方式获取标题
                    const titleSelectors = [
                        'h1',
                        '.document-title',
                        '.title',
                        '[data-testid="title"]'
                    ];

                    for (const selector of titleSelectors) {
                        const element = document.querySelector(selector);
                        if (element && element.innerText.trim()) {
                            return element.innerText.trim();
                        }
                    }

                    // 如果都找不到，使用页面标题
                    return document.title || 'Unknown Title';
                }
            """)
            return title if title else "Unknown Title"
        except:
            return "Unknown Title"

    async def _extract_author(self, page: Page) -> Optional[str]:
        """提取作者"""
        try:
            author = await page.evaluate("""
                () => {
                    // 尝试多种方式获取作者
                    const authorSelectors = [
                        '.author',
                        '.creator',
                        '[data-testid="author"]',
                        '.user-avatar'
                    ];

                    for (const selector of authorSelectors) {
                        const element = document.querySelector(selector);
                        if (element) {
                            const text = element.innerText || element.getAttribute('title');
                            if (text && text.trim()) {
                                return text.trim();
                            }
                        }
                    }

                    return null;
                }
            """)
            return author if author else None
        except:
            return None

    async def _extract_image_urls(self, page: Page) -> List[str]:
        """提取页面中的所有图片URL"""
        try:
            urls = await page.evaluate("""
                () => {
                    const images = document.querySelectorAll('img');
                    const urls = [];

                    for (const img of images) {
                        let src = img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src');

                        if (src) {
                            // 处理相对路径
                            if (src.startsWith('/')) {
                                src = window.location.origin + src;
                            }

                            // 过滤掉小图标和头像
                            const lowerSrc = src.toLowerCase();
                            if (!lowerSrc.includes('icon') &&
                                !lowerSrc.includes('avatar') &&
                                !lowerSrc.includes('logo') &&
                                !lowerSrc.includes('favicon')) {
                                urls.push(src);
                            }
                        }
                    }

                    // 去重
                    return [...new Set(urls)];
                }
            """)

            logger.info(f"Extracted {len(urls)} image URLs")
            return urls if urls else []
        except Exception as e:
            logger.error(f"Error extracting image URLs: {e}")
            return []


# 便捷函数
async def fetch_feishu_doc(url: str, cookies_str: Optional[str] = None) -> dict:
    """
    便捷函数：直接获取飞书文档

    Args:
        url: 飞书文档URL
        cookies_str: 可选的cookie字符串，格式: "key1=value1; key2=value2"
    """
    async with FeishuScraper(cookies_str=cookies_str) as scraper:
        return await scraper.fetch_doc(url)


async def get_feishu_doc_info(url: str, cookies_str: Optional[str] = None) -> dict:
    """
    便捷函数：获取文档基本信息

    Args:
        url: 飞书文档URL
        cookies_str: 可选的cookie字符串
    """
    async with FeishuScraper(cookies_str=cookies_str) as scraper:
        return await scraper.get_doc_info(url)
