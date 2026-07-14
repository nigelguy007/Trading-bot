#!/usr/bin/env python3
"""测试脚本"""

import asyncio
import sys
from pathlib import Path

# 添加 src 目录到路径
src_dir = Path(__file__).parent / 'src'
sys.path.insert(0, str(src_dir))

from scraper import FeishuScraper


async def test_basic():
    """测试基本功能"""
    print("=" * 50)
    print("测试 1：测试基本导入")
    print("=" * 50)

    try:
        from scraper import FeishuScraper
        from parser import FeishuParser
        from image_handler import ImageHandler
        print("✓ 所有模块导入成功")
        return True
    except ImportError as e:
        print(f"✗ 模块导入失败: {e}")
        return False


async def test_scraper_init():
    """测试爬虫初始化"""
    print("\n" + "=" * 50)
    print("测试 2：测试爬虫初始化")
    print("=" * 50)

    try:
        scraper = FeishuScraper()
        await scraper.init()
        print("✓ 爬虫初始化成功")
        await scraper.cleanup()
        return True
    except Exception as e:
        print(f"✗ 爬虫初始化失败: {e}")
        return False


async def test_parser():
    """测试解析器"""
    print("\n" + "=" * 50)
    print("测试 3：测试解析器")
    print("=" * 50)

    try:
        from parser import FeishuParser

        parser = FeishuParser()

        # 测试 HTML 解析
        html = """
        <h1>测试标题</h1>
        <p>这是一个测试段落。</p>
        <ul>
            <li>列表项 1</li>
            <li>列表项 2</li>
        </ul>
        """

        blocks = parser.parse_html(html)
        print(f"✓ 解析了 {len(blocks)} 个内容块")

        # 测试 Markdown 生成
        markdown = parser.generate_markdown(blocks, {})
        print("✓ Markdown 生成成功")
        print("\n生成的 Markdown:")
        print(markdown)

        return True
    except Exception as e:
        print(f"✗ 解析器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_image_handler():
    """测试图片处理器"""
    print("\n" + "=" * 50)
    print("测试 4：测试图片处理器")
    print("=" * 50)

    try:
        from image_handler import ImageHandler

        handler = ImageHandler()
        print(f"✓ 图片处理器初始化成功，目录: {handler.image_dir}")

        return True
    except Exception as e:
        print(f"✗ 图片处理器测试失败: {e}")
        return False


async def main():
    """主测试函数"""
    print("\n" + "=" * 50)
    print("飞书文档读取 MCP 服务器测试")
    print("=" * 50)

    tests = [
        test_basic,
        test_scraper_init,
        test_parser,
        test_image_handler
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if await test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"\n✗ 测试异常: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 50)
    print("测试总结")
    print("=" * 50)
    print(f"✓ 通过: {passed}")
    print(f"✗ 失败: {failed}")
    print(f"总计: {passed + failed}")

    if failed == 0:
        print("\n🎉 所有测试通过！")
        return 0
    else:
        print(f"\n⚠️  {failed} 个测试失败")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
