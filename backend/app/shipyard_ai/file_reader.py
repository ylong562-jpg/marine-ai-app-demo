"""

多格式文件读取器
支持读取文本、Word、Excel、PPT等文件格式
"""
import os
from typing import Optional, Dict, Any
import docx
from docx import Document
import openpyxl
from openpyxl import load_workbook


def read_text_file(file_path: str) -> str:
    """读取纯文本文件"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except UnicodeDecodeError:
        # 尝试其他编码
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()
            return content
        except:
            raise Exception("无法以UTF-8或GBK编码读取文件")


def read_word_file(file_path: str) -> str:
    """
    读取Word文档 (.docx)
    提取所有段落和表格中的文本
    """
    try:
        doc = Document(file_path)

        content_parts = []

        # 读取所有段落
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                content_parts.append(paragraph.text.strip())

        # 读取所有表格
        for table in doc.tables:
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    if cell.text.strip():
                        row_text.append(cell.text.strip())
                if row_text:
                    content_parts.append(' | '.join(row_text))

        # 合并所有内容
        content = '\n'.join(content_parts)
        return content

    except Exception as e:
        raise Exception(f"读取Word文档失败: {e}")


def read_excel_file(file_path: str) -> str:
    """
    读取Excel文件 (.xlsx, .xls)
    提取所有工作表中的数据
    """
    try:
        wb = load_workbook(file_path, data_only=True)

        content_parts = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]

            content_parts.append(f"\n=== Sheet: {sheet_name} ===\n")

            for row in ws.iter_rows(values_only=True):
                # 过滤掉空行
                row_data = [str(cell) if cell is not None else '' for cell in row]
                if any(cell.strip() for cell in row_data):
                    content_parts.append(' | '.join(row_data))

        content = '\n'.join(content_parts)
        return content

    except Exception as e:
        raise Exception(f"读取Excel文件失败: {e}")


def read_ppt_file(file_path: str) -> str:
    """
    读取PowerPoint文件 (.pptx)
    提取所有幻灯片中的文本
    """
    try:
        from pptx import Presentation

        prs = Presentation(file_path)

        content_parts = []

        for i, slide in enumerate(prs.slides, 1):
            content_parts.append(f"\n=== Slide {i} ===\n")

            # 提取所有形状中的文本
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    content_parts.append(shape.text.strip())

                # 如果是表格
                if shape.has_table:
                    table = shape.table
                    for row in table.rows:
                        row_text = []
                        for cell in row.cells:
                            if cell.text.strip():
                                row_text.append(cell.text.strip())
                        if row_text:
                            content_parts.append(' | '.join(row_text))

        content = '\n'.join(content_parts)
        return content

    except ImportError:
        raise Exception("请安装python-pptx库: pip install python-pptx")
    except Exception as e:
        raise Exception(f"读取PowerPoint文件失败: {e}")


def read_pdf_file(file_path: str) -> str:
    """
    读取PDF文件
    提取文本内容
    """
    try:
        import PyPDF2

        content_parts = []

        with open(file_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)

            for i, page in enumerate(pdf_reader.pages, 1):
                text = page.extract_text()
                if text.strip():
                    content_parts.append(f"\n=== Page {i} ===\n")
                    content_parts.append(text.strip())

        content = '\n'.join(content_parts)
        return content

    except ImportError:
        raise Exception("请安装PyPDF2库: pip install PyPDF2")
    except Exception as e:
        raise Exception(f"读取PDF文件失败: {e}")


def detect_file_type(file_path: str) -> str:
    """
    根据文件扩展名检测文件类型
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in ['.txt', '.text']:
        return 'text'
    elif ext in ['.doc', '.docx']:
        return 'word'
    elif ext in ['.xls', '.xlsx']:
        return 'excel'
    elif ext in ['.ppt', '.pptx']:
        return 'ppt'
    elif ext == '.pdf':
        return 'pdf'
    else:
        return 'unknown'


def read_file(file_path: str) -> Dict[str, Any]:
    """
    智能读取文件内容

    Args:
        file_path: 文件路径

    Returns:
        字典，包含:
        - success: 是否成功
        - content: 文件内容
        - file_type: 文件类型
        - error: 错误信息（如果失败）
    """

    # 检查文件是否存在
    if not os.path.exists(file_path):
        return {
            'success': False,
            'error': f"文件不存在: {file_path}",
            'content': None,
            'file_type': None
        }

    # 检测文件类型
    file_type = detect_file_type(file_path)

    if file_type == 'unknown':
        return {
            'success': False,
            'error': f"不支持的文件类型: {os.path.splitext(file_path)[1]}",
            'content': None,
            'file_type': None
        }

    # 读取文件
    try:
        if file_type == 'text':
            content = read_text_file(file_path)
        elif file_type == 'word':
            content = read_word_file(file_path)
        elif file_type == 'excel':
            content = read_excel_file(file_path)
        elif file_type == 'ppt':
            content = read_ppt_file(file_path)
        elif file_type == 'pdf':
            content = read_pdf_file(file_path)
        else:
            return {
                'success': False,
                'error': f"未实现的文件类型: {file_type}",
                'content': None,
                'file_type': file_type
            }

        return {
            'success': True,
            'content': content,
            'file_type': file_type,
            'error': None,
            'file_name': os.path.basename(file_path)
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'content': None,
            'file_type': file_type
        }


def read_multiple_files(file_paths: list) -> Dict[str, Any]:
    """
    批量读取多个文件

    Args:
        file_paths: 文件路径列表

    Returns:
        字典，包含每个文件的读取结果
    """
    results = {}

    for file_path in file_paths:
        result = read_file(file_path)
        results[file_path] = result

    return results


# 支持的文件类型列表
SUPPORTED_FILE_TYPES = {
    'text': ['.txt', '.text'],
    'word': ['.doc', '.docx'],
    'excel': ['.xls', '.xlsx'],
    'ppt': ['.ppt', '.pptx'],
    'pdf': ['.pdf']
}


def get_supported_extensions() -> list:
    """获取所有支持的文件扩展名"""
    extensions = []
    for file_types in SUPPORTED_FILE_TYPES.values():
        extensions.extend(file_types)
    return extensions


if __name__ == "__main__":
    # 测试代码
    import sys

    if len(sys.argv) < 2:
        print("用法: python file_reader.py <file_path>")
        print(f"\n支持的文件类型: {', '.join(get_supported_extensions())}")
        sys.exit(1)

    file_path = sys.argv[1]

    print(f"正在读取文件: {file_path}")
    print("=" * 80)

    result = read_file(file_path)

    if result['success']:
        print(f"✓ 文件类型: {result['file_type']}")
        print(f"✓ 内容长度: {len(result['content'])} 字符")
        print("\n内容预览（前500字符）:")
        print("-" * 80)
        print(result['content'][:500])
        print("-" * 80)
    else:
        print(f"✗ 读取失败: {result['error']}")
