#!/bin/bash

# 飞书文档读取 MCP 服务器安装脚本

set -e

echo "======================================"
echo "  飞书文档读取 MCP 服务器安装"
echo "======================================"
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python3 未安装${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Python 版本: $(python3 --version)"

# 检查 pip
if ! command -v pip3 &> /dev/null; then
    echo -e "${RED}❌ pip3 未安装${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} pip 版本: $(pip3 --version)"

# 1. 安装 Python 依赖
echo ""
echo -e "${YELLOW}[1/3]${NC} 安装 Python 依赖..."
pip3 install -r requirements.txt

echo -e "${GREEN}✓${NC} Python 依赖安装完成"

# 2. 安装 Playwright 浏览器
echo ""
echo -e "${YELLOW}[2/3]${NC} 安装 Playwright 浏览器..."

if ! command -v playwright &> /dev/null; then
    echo "安装 Playwright CLI..."
    pip3 install playwright

    echo "安装浏览器..."
    playwright install chromium
else
    echo "Playwright 已安装，更新浏览器..."
    playwright install chromium
fi

echo -e "${GREEN}✓${NC} Playwright 浏览器安装完成"

# 3. 创建临时目录
echo ""
echo -e "${YELLOW}[3/3]${NC} 创建临时目录..."
mkdir -p /tmp/feishu_docs
mkdir -p /tmp/feishu_images

echo -e "${GREEN}✓${NC} 临时目录创建完成"

# 测试安装
echo ""
echo -e "${YELLOW}测试安装...${NC}"

python3 -c "
import sys
sys.path.insert(0, 'src')

try:
    from scraper import FeishuScraper
    from parser import FeishuParser
    from image_handler import ImageHandler
    print('✓ 所有模块导入成功')
except ImportError as e:
    print(f'✗ 模块导入失败: {e}')
    sys.exit(1)
"

echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}安装完成！${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo "下一步："
echo "1. 配置 MCP 服务器（在 Claude Code 配置文件中）"
echo "2. 重启 Claude Code"
echo "3. 开始使用！"
echo ""
echo "配置文件示例："
cat << 'EOF'

{
  "mcpServers": {
    "feishu-reader": {
      "command": "python",
      "args": [
        "/path/to/feishu-read-mcp/src/server.py"
      ]
    }
  }
}

EOF

echo ""
echo "详细文档请查看 README.md"
