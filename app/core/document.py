"""文档处理模块 - 负责文档解析、分块"""
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class DocumentChunk:
    """文档分块"""
    content: str
    metadata: dict
    chunk_id: Optional[str] = None


class DocumentParser:
    """文档解析器 - 支持多种格式"""

    SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".md", ".txt", ".csv"}

    @staticmethod
    def parse(file_path: str) -> list[DocumentChunk]:
        """解析文档，返回文档块列表"""
        path = Path(file_path)
        ext = path.suffix.lower()

        if ext == ".pdf":
            return DocumentParser._parse_pdf(path)
        elif ext == ".docx":
            return DocumentParser._parse_docx(path)
        elif ext == ".xlsx":
            return DocumentParser._parse_excel(path)
        elif ext == ".md":
            return DocumentParser._parse_markdown(path)
        elif ext in (".txt", ".csv"):
            return DocumentParser._parse_text(path)
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    @staticmethod
    def _parse_pdf(path: Path) -> list[DocumentChunk]:
        """解析 PDF 文件"""
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("请安装 pypdf: pip install pypdf")

        reader = PdfReader(str(path))
        chunks = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                chunks.append(DocumentChunk(
                    content=text.strip(),
                    metadata={
                        "source": str(path),
                        "filename": path.name,
                        "page": i + 1,
                        "type": "pdf",
                    }
                ))
        return chunks

    @staticmethod
    def _parse_docx(path: Path) -> list[DocumentChunk]:
        """解析 Word 文档"""
        try:
            from docx import Document
        except ImportError:
            raise ImportError("请安装 python-docx: pip install python-docx")

        doc = Document(str(path))
        full_text = "\n".join(
            para.text for para in doc.paragraphs if para.text.strip()
        )
        if full_text.strip():
            return [DocumentChunk(
                content=full_text.strip(),
                metadata={
                    "source": str(path),
                    "filename": path.name,
                    "type": "docx",
                }
            )]
        return []

    @staticmethod
    def _parse_excel(path: Path) -> list[DocumentChunk]:
        """解析 Excel 文件"""
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ImportError("请安装 openpyxl: pip install openpyxl")

        wb = load_workbook(str(path), read_only=True)
        chunks = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                row_text = " | ".join(str(cell) for cell in row if cell is not None)
                if row_text.strip():
                    rows.append(row_text)
            if rows:
                chunks.append(DocumentChunk(
                    content="\n".join(rows),
                    metadata={
                        "source": str(path),
                        "filename": path.name,
                        "sheet": sheet_name,
                        "type": "xlsx",
                    }
                ))
        wb.close()
        return chunks

    @staticmethod
    def _parse_markdown(path: Path) -> list[DocumentChunk]:
        """解析 Markdown 文件"""
        text = path.read_text(encoding="utf-8")
        if text.strip():
            return [DocumentChunk(
                content=text.strip(),
                metadata={
                    "source": str(path),
                    "filename": path.name,
                    "type": "markdown",
                }
            )]
        return []

    @staticmethod
    def _parse_text(path: Path) -> list[DocumentChunk]:
        """解析纯文本文件"""
        text = path.read_text(encoding="utf-8")
        if text.strip():
            return [DocumentChunk(
                content=text.strip(),
                metadata={
                    "source": str(path),
                    "filename": path.name,
                    "type": "text",
                }
            )]
        return []


class TextSplitter:
    """文本分块器"""

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, chunks: list[DocumentChunk]) -> list[DocumentChunk]:
        """将文档块进一步切分为更小的块"""
        result = []
        for chunk in chunks:
            sub_chunks = self._split_text(chunk.content)
            for i, sub_text in enumerate(sub_chunks):
                result.append(DocumentChunk(
                    content=sub_text,
                    metadata={**chunk.metadata, "chunk_index": i},
                ))
        return result

    def _split_text(self, text: str) -> list[str]:
        """按字符长度分块，保留重叠"""
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]
            chunks.append(chunk)
            start = end - self.chunk_overlap
        return chunks
