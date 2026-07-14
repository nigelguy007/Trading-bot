"""飞书文档内容解析器"""

import re
import logging
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup, Tag, NavigableString
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


class FeishuParser:
    """飞书文档 HTML 解析器"""

    def __init__(self):
        # 映射图片URL到本地路径
        self.image_map: Dict[str, str] = {}

    def parse_html(self, html: str) -> List[Dict]:
        """
        解析 HTML 为结构化数据

        Args:
            html: HTML 字符串

        Returns:
            List[Dict]: 内容块列表
        """
        soup = BeautifulSoup(html, 'html.parser')
        blocks = []

        # 处理所有内容块
        for element in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                     'ul', 'ol', 'blockquote', 'pre', 'table']):
            block = self._parse_element(element)
            if block:
                blocks.append(block)

        return blocks

    def _parse_element(self, element) -> Optional[Dict]:
        """
        解析单个元素

        Args:
            element: BeautifulSoup 元素

        Returns:
            Dict: 解析后的内容块
        """
        tag_name = element.name.lower()

        # 标题
        if tag_name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(tag_name[1])
            text = self._clean_text(element)
            return {
                'type': 'heading',
                'level': level,
                'text': text
            }

        # 段落
        if tag_name == 'p':
            text = self._clean_text(element)
            if text.strip():
                return {
                    'type': 'paragraph',
                    'text': text
                }

        # 列表
        if tag_name in ['ul', 'ol']:
            items = []
            for li in element.find_all('li', recursive=False):
                item_text = self._clean_text(li)
                if item_text.strip():
                    items.append(item_text)

            return {
                'type': 'list',
                'ordered': tag_name == 'ol',
                'items': items
            }

        # 引用
        if tag_name == 'blockquote':
            text = self._clean_text(element)
            return {
                'type': 'quote',
                'text': text
            }

        # 代码块
        if tag_name == 'pre':
            code_text = element.get_text(strip=False)
            return {
                'type': 'code',
                'language': self._detect_language(element),
                'text': code_text
            }

        # 表格
        if tag_name == 'table':
            return self._parse_table(element)

        # 图片
        if tag_name == 'img':
            src = element.get('src', '')
            alt = element.get('alt', '')
            return {
                'type': 'image',
                'src': src,
                'alt': alt
            }

        # 链接
        if tag_name == 'a':
            href = element.get('href', '')
            text = self._clean_text(element)
            return {
                'type': 'link',
                'href': href,
                'text': text
            }

        return None

    def _parse_table(self, table) -> Dict:
        """
        解析表格

        Args:
            table: 表格元素

        Returns:
            Dict: 表格数据
        """
        rows = []

        # 获取表头
        thead = table.find('thead')
        header_row = None
        if thead:
            header_row = thead.find('tr')
            if header_row:
                headers = [self._clean_text(th) for th in header_row.find_all(['th', 'td'])]
                rows.append(headers)

        # 获取表格内容
        tbody = table.find('tbody')
        if tbody:
            for tr in tbody.find_all('tr'):
                row = [self._clean_text(td) for td in tr.find_all(['td', 'th'])]
                if row:
                    rows.append(row)

        # 如果没有 tbody，直接解析所有行
        if not tbody:
            for tr in table.find_all('tr'):
                row = [self._clean_text(td) for td in tr.find_all(['td', 'th'])]
                if row:
                    rows.append(row)

        return {
            'type': 'table',
            'rows': rows
        }

    def _clean_text(self, element) -> str:
        """
        清理元素文本

        Args:
            element: BeautifulSoup 元素

        Returns:
            str: 清理后的文本
        """
        # 获取纯文本，保留换行
        text = element.get_text(separator=' ', strip=True)

        # 清理多余的空白字符
        text = re.sub(r'\s+', ' ', text)

        return text

    def _detect_language(self, element) -> str:
        """
        检测代码语言

        Args:
            element: 代码块元素

        Returns:
            str: 语言名称
        """
        # 尝试从 class 或其他属性获取语言信息
        classes = element.get('class', [])
        for cls in classes:
            if 'language-' in cls:
                return cls.replace('language-', '')

        # 尝试从父元素获取
        parent = element.parent
        if parent:
            classes = parent.get('class', [])
            for cls in classes:
                if 'language-' in cls:
                    return cls.replace('language-', '')

        return ''

    def generate_markdown(self, blocks: List[Dict], image_map: Dict[str, str]) -> str:
        """
        生成 Markdown

        Args:
            blocks: 内容块列表
            image_map: 图片映射 {URL: 本地路径}

        Returns:
            str: Markdown 字符串
        """
        markdown_parts = []
        self.image_map = image_map

        for block in blocks:
            md = self._block_to_markdown(block)
            if md:
                markdown_parts.append(md)

        return '\n\n'.join(markdown_parts)

    def _block_to_markdown(self, block: Dict) -> str:
        """
        将内容块转换为 Markdown

        Args:
            block: 内容块

        Returns:
            str: Markdown 字符串
        """
        block_type = block.get('type')

        if block_type == 'heading':
            level = block.get('level', 1)
            text = block.get('text', '')
            return f"{'#' * level} {text}"

        if block_type == 'paragraph':
            return block.get('text', '')

        if block_type == 'list':
            items = block.get('items', [])
            ordered = block.get('ordered', False)
            md_items = []

            for i, item in enumerate(items):
                if ordered:
                    md_items.append(f"{i + 1}. {item}")
                else:
                    md_items.append(f"- {item}")

            return '\n'.join(md_items)

        if block_type == 'quote':
            text = block.get('text', '')
            # 添加 > 前缀
            lines = text.split('\n')
            quoted_lines = [f"> {line}" if line.strip() else ">" for line in lines]
            return '\n'.join(quoted_lines)

        if block_type == 'code':
            language = block.get('language', '')
            text = block.get('text', '')

            if language:
                return f"```{language}\n{text}\n```"
            else:
                return f"```\n{text}\n```"

        if block_type == 'table':
            return self._table_to_markdown(block)

        if block_type == 'image':
            src = block.get('src', '')
            alt = block.get('alt', '')

            # 获取本地路径
            local_path = self.image_map.get(src, src)

            if alt:
                return f"![{alt}]({local_path})"
            else:
                return f"![]({local_path})"

        if block_type == 'link':
            href = block.get('href', '')
            text = block.get('text', '')

            if text == href:
                return f"<{href}>"
            else:
                return f"[{text}]({href})"

        return ''

    def _table_to_markdown(self, block: Dict) -> str:
        """
        将表格转换为 Markdown

        Args:
            block: 表格数据

        Returns:
            str: Markdown 表格
        """
        rows = block.get('rows', [])
        if not rows:
            return ''

        markdown_lines = []

        # 表头
        if rows:
            markdown_lines.append('| ' + ' | '.join(rows[0]) + ' |')

        # 分隔线
        if rows:
            markdown_lines.append('| ' + ' | '.join(['---'] * len(rows[0])) + ' |')

        # 表格内容
        for row in rows[1:]:
            markdown_lines.append('| ' + ' | '.join(row) + ' |')

        return '\n'.join(markdown_lines)

    def extract_text(self, blocks: List[Dict]) -> str:
        """
        提取纯文本

        Args:
            blocks: 内容块列表

        Returns:
            str: 纯文本
        """
        text_parts = []

        for block in blocks:
            block_type = block.get('type')

            if block_type == 'heading':
                text_parts.append(block.get('text', ''))

            elif block_type == 'paragraph':
                text_parts.append(block.get('text', ''))

            elif block_type == 'list':
                items = block.get('items', [])
                text_parts.extend(items)

            elif block_type == 'quote':
                text_parts.append(block.get('text', ''))

            elif block_type == 'code':
                text_parts.append(block.get('text', ''))

            elif block_type == 'table':
                rows = block.get('rows', [])
                for row in rows:
                    text_parts.extend(row)

            elif block_type == 'image':
                alt = block.get('alt', '')
                if alt:
                    text_parts.append(alt)

        return ' '.join(text_parts)

    def extract_images(self, blocks: List[Dict]) -> List[str]:
        """
        提取所有图片URL

        Args:
            blocks: 内容块列表

        Returns:
            List[str]: 图片URL列表
        """
        images = []

        for block in blocks:
            if block.get('type') == 'image':
                src = block.get('src', '')
                if src:
                    images.append(src)

        return images
