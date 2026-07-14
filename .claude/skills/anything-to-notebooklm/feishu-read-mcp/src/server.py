"""MCP服务器主入口"""

import sys
from pathlib import Path

# 添加src目录到Python路径，支持直接运行
if __name__ == "__main__":
    src_dir = Path(__file__).parent
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

from fastmcp import FastMCP
import logging

# 支持相对导入和绝对导入
try:
    from .scraper import FeishuScraper
except ImportError:
    from scraper import FeishuScraper

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 初始化MCP服务
mcp = FastMCP("feishu-reader")

# 初始化爬虫（全局单例）
scraper = FeishuScraper()


@mcp.tool()
async def read_feishu_doc(url: str, cookies_str: Optional[str] = None) -> dict:
    """
    读取飞书文档内容并转换为 Markdown（支持图片）

    Args:
        url: 飞书文档URL，格式:
            - https://bytedance.feishu.cn/docs/docc/xxx
            - https://xxx.feishu.cn/docs/docc/xxx
        cookies_str: 可选的cookie字符串，格式: "key1=value1; key2=value2"
                      如果提供，可以下载需要认证的图片

    Returns:
        dict: {
            "success": bool,
            "title": str,
            "author": str,
            "content": str,  # Markdown 格式
            "images": List[str],  # 本地图片路径列表
            "word_count": int,
            "error": str | None
        }
    """
    try:
        # URL验证
        if not ("feishu.cn" in url or "feishu.com" in url):
            return {
                "success": False,
                "error": "Invalid URL format. Must be a Feishu document URL."
            }

        logger.info(f"Fetching Feishu document: {url}")

        # 使用cookie创建新的scraper实例
        scraper_instance = FeishuScraper(cookies_str=cookies_str)
        await scraper_instance.init()

        try:
            # 调用爬虫获取内容
            result = await scraper_instance.fetch_doc(url)

            if result.get("success"):
                logger.info(f"Successfully fetched: {result.get('title', 'Unknown')}")
                logger.info(f"Extracted {result.get('word_count', 0)} words and {len(result.get('images', []))} images")
            else:
                logger.error(f"Failed to fetch: {result.get('error')}")

            return result
        finally:
            await scraper_instance.cleanup()

    except Exception as e:
        logger.error(f"Error fetching Feishu document: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def get_doc_info(url: str) -> dict:
    """
    获取飞书文档的基本信息（不下载内容）

    Args:
        url: 飞书文档URL

    Returns:
        dict: {
            "success": bool,
            "title": str,
            "author": str,
            "word_count": int,
            "image_count": int,
            "error": str | None
        }
    """
    try:
        result = await scraper.get_doc_info(url)
        return result
    except Exception as e:
        logger.error(f"Error getting doc info: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


# 清理函数
async def cleanup():
    """清理资源"""
    await scraper.cleanup()


if __name__ == "__main__":
    # 支持命令行直接运行
    mcp.run()
