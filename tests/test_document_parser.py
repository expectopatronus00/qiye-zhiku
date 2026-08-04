"""文档解析器单元测试 - Day 5 (PDF 版面分析 / 表格识别 / 图片 OCR)"""
import sys
from pathlib import Path

import pytest

from app.core.config import DocumentConfig
from app.core.document import DocumentParser, TextSplitter

# 确保 tests/make_layout_pdf.py 可导入(生成测试 PDF)
sys.path.insert(0, str(Path(__file__).parent))
from make_layout_pdf import OUT as LAYOUT_PDF_PATH  # noqa: E402


@pytest.fixture(scope="module")
def layout_pdf():
    """生成含标题/正文/表格/图片的测试 PDF"""
    from make_layout_pdf import main
    main()
    assert Path(LAYOUT_PDF_PATH).exists(), "测试 PDF 生成失败"
    return LAYOUT_PDF_PATH


@pytest.fixture(scope="module")
def parsed(layout_pdf):
    parser = DocumentParser()
    return parser.parse(layout_pdf)


def _chunks_by_type(chunks, block_type: str) -> list:
    return [c for c in chunks if c.metadata.get("block_type") == block_type]


class TestPdfLayout:
    """PDF 版面分析: 标题/正文识别"""

    def test_heading_detected(self, parsed):
        headings = _chunks_by_type(parsed, "heading")
        texts = [c.content for c in headings]
        assert any("服务器硬件监控方案" in t for t in texts), "大字号标题应识别为 heading"
        assert any("监控阈值表" in t for t in texts), "小节标题应识别为 heading"

    def test_body_detected(self, parsed):
        bodies = _chunks_by_type(parsed, "body")
        texts = [c.content for c in bodies]
        assert any("7x24小时监控" in t for t in texts), "正文应识别为 body"

    def test_heading_has_font_size_metadata(self, parsed):
        headings = _chunks_by_type(parsed, "heading")
        assert headings, "应存在 heading 块"
        for h in headings:
            assert h.metadata.get("font_size", 0) >= 13, "heading 字号应 >= 配置阈值"
            assert h.metadata.get("page") == 1

    def test_page_metadata(self, parsed):
        for c in parsed:
            assert c.metadata["type"] == "pdf"
            assert c.metadata["page"] == 1
            assert c.metadata["filename"].endswith(".pdf")
            assert c.metadata.get("bbox"), "应有 bbox 供前端定位"

    def test_heading_threshold_config(self, layout_pdf):
        """提高阈值后, 15pt 小节标题应降级为 body"""
        parser = DocumentParser(config=DocumentConfig(heading_min_size=18))
        chunks = parser.parse(layout_pdf)
        bodies = _chunks_by_type(chunks, "body")
        texts = [c.content for c in bodies]
        assert any("监控阈值表" in t for t in texts), "15pt 标题在 18pt 阈值下应为 body"


class TestTableExtraction:
    """表格识别转 Markdown"""

    def test_table_to_markdown(self, parsed):
        tables = _chunks_by_type(parsed, "table")
        assert tables, "应识别出表格"
        content = tables[0].content
        assert "| --- |" in content, "表格应包含 Markdown 分隔行"
        assert "CPU温度" in content
        assert "40-70C" in content

    def test_table_text_not_duplicated_in_body(self, parsed):
        """表格区域内的文本不应再作为 body 重复入库"""
        bodies = _chunks_by_type(parsed, "body")
        texts = [c.content for c in bodies]
        assert not any("正常区间" in t for t in texts), "表格表头不应重复出现在 body"
        assert not any("告警阈值" in t for t in texts)

    def test_table_disabled(self, layout_pdf):
        parser = DocumentParser(config=DocumentConfig(table_to_markdown=False))
        chunks = parser.parse(layout_pdf)
        assert not _chunks_by_type(chunks, "table"), "禁用后不应有 table 块"


class TestOcr:
    """内嵌图片 OCR"""

    def test_ocr_extracts_image_text(self, parsed):
        ocrs = _chunks_by_type(parsed, "ocr")
        assert ocrs, "图片内容应被 OCR 提取"
        content = ocrs[0].content
        assert "GPU Utilization" in content, f"应识别图片中的文字, 实际: {content[:80]}"
        assert content.startswith("[图片内容"), "OCR 块应带图片内容标记"
        assert "GPU 监控截图" in content, "OCR 块应拼接所在章节标题作为检索上下文"

    def test_ocr_disabled(self, layout_pdf):
        parser = DocumentParser(config=DocumentConfig(ocr_enabled=False))
        chunks = parser.parse(layout_pdf)
        assert not _chunks_by_type(chunks, "ocr"), "禁用后不应有 ocr 块"


class TestFallback:
    """降级与兼容性"""

    def test_plain_pdf_fallback(self, layout_pdf):
        """pypdf 纯文本降级路径仍可工作"""
        chunks = DocumentParser._parse_pdf_plain(Path(layout_pdf))
        assert chunks, "pypdf 降级应返回文本块"
        assert all("type" in c.metadata and c.metadata["type"] == "pdf" for c in chunks)

    def test_markdown_still_works(self, tmp_path):
        md = tmp_path / "a.md"
        md.write_text("# 标题\n\n正文内容", encoding="utf-8")
        chunks = DocumentParser().parse(str(md))
        assert len(chunks) == 1
        assert chunks[0].metadata["type"] == "markdown"

    def test_text_splitter_preserves_metadata(self, parsed):
        """切分后 metadata 应保留 block_type 与 page"""
        splitter = TextSplitter(chunk_size=50, chunk_overlap=10)
        sub = splitter.split(parsed)
        assert len(sub) > len(parsed)
        for c in sub:
            assert c.metadata.get("block_type") in ("heading", "body", "table", "ocr")
            assert c.metadata.get("page") == 1
