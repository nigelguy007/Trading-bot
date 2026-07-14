# 飞书文档读取 MCP 服务器

一个 MCP（Model Context Protocol）服务器，用于读取飞书文档并转换为 Markdown 格式，支持图片下载。

## ✨ 特性

- 📖 **读取飞书文档** - 支持所有飞书文档格式
- 🖼️ **图片下载** - 自动下载文档中的图片并保存到本地
- 📝 **Markdown 输出** - 完美保留文档结构和格式
- ⚡ **高性能** - 异步处理，支持并发
- 🔧 **易于集成** - 符合 MCP 标准，可直接集成到 Claude Code

## 🚀 安装

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 Playwright 浏览器

```bash
playwright install chromium
```

### 3. 配置 MCP

在 Claude Code 配置文件中添加：

```json
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
```

## 📚 使用方法

### 作为 MCP 工具使用

#### 1. 读取飞书文档

```python
result = await read_feishu_doc("https://bytedance.feishu.cn/docs/docc/xxx")
```

返回结果：

```python
{
    "success": True,
    "title": "文档标题",
    "author": "作者",
    "content": "# 文档标题\n\n这是文档内容...",
    "images": ["/tmp/feishu_images/xxx1.png", "/tmp/feishu_images/xxx2.png"],
    "word_count": 1500,
    "image_count": 2,
    "url": "原始URL"
}
```

#### 2. 获取文档信息

```python
info = await get_doc_info("https://bytedance.feishu.cn/docs/docc/xxx")
```

返回结果：

```python
{
    "success": True,
    "title": "文档标题",
    "author": "作者",
    "word_count": 1500,
    "image_count": 2
}
```

### 直接作为 Python 库使用

```python
from src.scraper import FeishuScraper

# 读取文档
async with FeishuScraper() as scraper:
    result = await scraper.fetch_doc("https://bytedance.feishu.cn/docs/docc/xxx")
    print(result["content"])
    print(f"下载了 {len(result['images'])} 张图片")
```

## 📦 支持的格式

### 输入格式
- ✅ 飞书文档（feishu.cn / feishu.com）
- ✅ 公开文档
- ✅ 需要登录的文档（支持 Cookie）

### 输出格式
- ✅ Markdown（.md）
- ✅ 保留标题层级
- ✅ 保留列表结构
- ✅ 保留表格
- ✅ 保留代码块
- ✅ 保留图片（本地路径）
- ✅ 保留链接
- ✅ 保留引用

### 图片处理
- ✅ 自动下载图片
- ✅ 支持 JPG、PNG、GIF、WebP 等格式
- ✅ 图片优化（压缩、格式转换）
- ✅ 本地存储（/tmp/feishu_images/）

## 🔧 高级配置

### 图片存储路径

默认图片存储在 `/tmp/feishu_images/`，可以通过修改 `ImageHandler` 类来更改：

```python
image_handler = ImageHandler()
image_handler.image_dir = Path("/custom/path/to/images")
```

### 并发控制

默认最多并发 5 个图片下载任务，可以通过修改 `scraper.py` 中的 semaphore 来调整：

```python
semaphore = asyncio.Semaphore(10)  # 改为 10
```

### 超时设置

默认下载超时 30 秒，可以通过修改 `image_handler.py` 中的 `timeout` 来调整：

```python
self.timeout = ClientTimeout(total=60, connect=20)  # 改为 60 秒
```

## 📝 示例

### 示例 1：读取简单的飞书文档

```python
from src.scraper import fetch_feishu_doc

result = await fetch_feishu_doc(
    "https://bytedance.feishu.cn/docs/docc/abc123"
)

if result["success"]:
    print(f"标题: {result['title']}")
    print(f"作者: {result['author']}")
    print(f"内容:\n{result['content']}")
    print(f"图片: {result['images']}")
else:
    print(f"错误: {result['error']}")
```

### 示例 2：批量处理多个文档

```python
from src.scraper import FeishuScraper

async def batch_process(urls):
    async with FeishuScraper() as scraper:
        for url in urls:
            result = await scraper.fetch_doc(url)
            # 处理结果...
```

### 示例 3：自定义图片处理

```python
from src.image_handler import ImageHandler

async def download_with_custom_handler(urls):
    handler = ImageHandler()
    handler.image_dir = Path("./my_images")  # 自定义目录

    image_map = await handler.download_images(urls)
    return image_map
```

## 🐛 故障排除

### 问题 1：Playwright 安装失败

```bash
# 卸载并重新安装
pip uninstall playwright
pip install playwright

# 安装浏览器
playwright install chromium
```

### 问题 2：图片下载失败

检查：
- 网络连接
- 图片 URL 是否可访问
- 临时目录权限

```bash
ls -la /tmp/feishu_images/
```

### 问题 3：文档加载超时

增加超时时间：

```python
await page.goto(url, wait_until='networkidle', timeout=120000)  # 改为 120 秒
```

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📧 联系方式

- GitHub: https://github.com/joeseesun/qiaomu-anything-to-notebooklm
- Email: joe@example.com

---

**由 [joeseesun](https://github.com/joeseesun) 开发**
