"""

生成船厂介绍Word文档（严谨和全面）- 带页眉Logo版
"""
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
import json
from datetime import datetime
from typing import Optional, Dict, Any
import os

# Logo路径配置
DEFAULT_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')

def safe_get_value(data_dict, key, default='N/A'):
    """
    安全获取字典值，处理None值情况
    """
    value = data_dict.get(key)
    if value is None or value == 'null' or value == '':
        return default
    return str(value)


def _format_annual_capacity(value):
    """将年产能 JSON 解析为 (vessel, specs) 两个显示值"""
    if not value or value == 'N/A':
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return value, None
    if isinstance(value, dict):
        return value.get('vessel'), value.get('specs')
    return str(value), None

def safe_set_cell_text(cell, value, default='N/A'):
    """
    安全设置单元格文本，彻底解决None值问题
    """
    if value is None or value == 'null' or value == '':
        cell.text = default
    else:
        cell.text = str(value)


def add_header_logo(doc, logo_path=None):
    """
    在文档右上角添加Logo到页眉 - 修复版
    """
    if logo_path is None:
        logo_path = DEFAULT_LOGO_PATH
    try:
        # 获取文档的第一个section[5](@ref)
        section = doc.sections[0]
        header = section.header

        # 首先检查页眉是否链接到前一节[3,5](@ref)
        if header.is_linked_to_previous:
            # 创建新的页眉定义[5](@ref)
            header.is_linked_to_previous = False

        # 确保页眉有段落，如果没有则创建[1](@ref)
        if len(header.paragraphs) == 0:
            header_paragraph = header.add_paragraph()
        else:
            header_paragraph = header.paragraphs[0]

        # 清除段落中原有的所有内容[2](@ref)
        header_paragraph.clear()

        # 设置段落对齐方式为右对齐[1](@ref)
        header_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

        # 调整段落格式，确保Logo位置合适[2,6](@ref)
        # 使用适度的缩进而不是负缩进，避免Logo超出页面
        header_paragraph.paragraph_format.left_indent = 0
        header_paragraph.paragraph_format.right_indent = 0
        header_paragraph.paragraph_format.space_before = Pt(0)  # 移除段前间距
        header_paragraph.paragraph_format.space_after = Pt(0)  # 移除段后间距

        # 添加Logo图片[1,4](@ref)
        run = header_paragraph.add_run()

        # 设置Logo大小（适度大小，避免过大）[7](@ref)
        logo_width = Inches(1.5)  # 调整为0.8英寸，更合适的尺寸
        run.add_picture(logo_path, width=logo_width)

        print("✅ Logo已成功添加到页眉右上角")
        return True

    except Exception as e:
        print(f"❌ 添加页眉Logo失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_word_document(
    shipyard_data: Dict[str, Any],
    output_path: str,
    logo_path: str = DEFAULT_LOGO_PATH,
):
    """
    创建船厂介绍Word文档 - 带页眉Logo版
    """

    doc = Document()

    # ===== 添加页眉Logo =====

    add_header_logo(doc, logo_path=logo_path)


    # 设置文档默认字体
    doc.styles['Normal'].font.name = 'Arial'
    doc.styles['Normal']._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
    doc.styles['Normal'].font.size = Pt(10.5)

    # ===== 1. 标题页 =====
    title = doc.add_heading(safe_get_value(shipyard_data, 'English_name', 'Shipyard'), level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title.runs[0]
    title_run.font.size = Pt(24)
    title_run.font.bold = True
    title_run.font.color.rgb = RGBColor(0, 51, 102)

    subtitle = doc.add_paragraph('Shipyard Profile & Introduction')
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle_run = subtitle.runs[0]
    subtitle_run.font.size = Pt(16)
    subtitle_run.font.color.rgb = RGBColor(0, 51, 102)

    # ===== 2. 企业概况 =====
    doc.add_heading('1. Company Overview', level=1)

    overview_para = doc.add_paragraph()
    overview_text = f"{safe_get_value(shipyard_data, 'name', 'The shipyard')} is a "

    if shipyard_data.get('is_state_owned') == 'Y':
        overview_text += "state-owned "
    else:
        overview_text += "privately-owned "

    overview_text += f"shipbuilding enterprise located in {safe_get_value(shipyard_data, 'location', 'China')}"

    establishment_date = shipyard_data.get('establishment_date')
    if establishment_date and establishment_date != 'null':
        try:
            est_year = str(establishment_date)[:4]
            overview_text += f", established in {est_year}"
        except:
            pass

    overview_text += "."

    ubo = shipyard_data.get('ubo')
    if ubo and ubo != 'null':
        overview_text += f" The shipyard is part of {ubo} group."

    specialties = shipyard_data.get('specialties')
    if specialties and specialties != 'null':
        overview_text += f" {specialties}"

    overview_para.add_run(overview_text)
    overview_para.paragraph_format.line_spacing = 1.5
    overview_para.paragraph_format.space_after = Pt(12)

    doc.add_paragraph()

    # ===== 3. 基本信息表 =====
    doc.add_heading('2. Basic Information', level=1)

    table = doc.add_table(rows=9, cols=2)
    table.style = 'Light Grid Accent 1'

    # 填充基本信息表
    active_status = shipyard_data.get('is_active')
    if active_status == 'Y':
        active_str = 'Active'
    elif active_status == 'N':
        active_str = 'Inactive / Closed'
    else:
        active_str = safe_get_value(shipyard_data, 'is_active', 'N/A')

    type_str = safe_get_value(shipyard_data, 'shipyard_type', 'N/A')

    basic_info_rows = [
        ('Shipyard Name', safe_get_value(shipyard_data, 'name')),
        ('Location', safe_get_value(shipyard_data, 'location')),
        ('Establishment Date', safe_get_value(shipyard_data, 'establishment_date')),
        ('Country', safe_get_value(shipyard_data, 'country')),
        ('Ownership', 'State-owned' if shipyard_data.get('is_state_owned') == 'Y' else 'Private'),
        ('Active Status', active_str),
        ('Shipyard Type', type_str.capitalize() if type_str != 'N/A' else 'N/A'),
        ('Parent Company (UBO)', safe_get_value(shipyard_data, 'ubo')),
        ('Update Time', safe_get_value(shipyard_data, 'update_time'))
    ]

    for i, (label, value) in enumerate(basic_info_rows):
        # 使用安全方法设置单元格文本
        safe_set_cell_text(table.rows[i].cells[0], label)
        safe_set_cell_text(table.rows[i].cells[1], value)

        # 设置标签列为粗体
        table.rows[i].cells[0].paragraphs[0].runs[0].font.bold = True
        table.rows[i].cells[0].paragraphs[0].runs[0].font.color.rgb = RGBColor(0, 51, 102)

    doc.add_paragraph()

    # ===== 4. 设施与能力 =====
    doc.add_heading('3. Facilities and Capacity', level=1)

    # 3.1 规模
    doc.add_heading('3.1 Shipyard Scale', level=2)

    scale_table = doc.add_table(rows=3, cols=2)
    scale_table.style = 'Light Grid Accent 1'

    shipyard_area = safe_get_value(shipyard_data, 'shipyard_area')
    if shipyard_area and shipyard_area != 'N/A':
        try:
            shipyard_area = f"{int(shipyard_area):,} m²"
        except:
            pass

    shoreline = safe_get_value(shipyard_data, 'total_shoreline_length')
    if shoreline and shoreline != 'N/A':
        try:
            shoreline = f"{int(shoreline):,} m"
        except:
            pass

    scale_rows = [
        ('Shipyard Area', shipyard_area),
        ('Total Shoreline Length', shoreline),
        ('Number of Employees', safe_get_value(shipyard_data, 'employee_count'))
    ]

    for i, (label, value) in enumerate(scale_rows):
        safe_set_cell_text(scale_table.rows[i].cells[0], label)
        safe_set_cell_text(scale_table.rows[i].cells[1], value)
        scale_table.rows[i].cells[0].paragraphs[0].runs[0].font.bold = True

    doc.add_paragraph()

    # 3.2 生产线参数
    doc.add_heading('3.2 Production Line Parameters', level=2)

    production_lines = shipyard_data.get('production_lines')
    if production_lines and production_lines != 'null':
        if isinstance(production_lines, str):
            try:
                production_lines = json.loads(production_lines)
            except:
                production_lines = None

        if production_lines and isinstance(production_lines, list) and len(production_lines) > 0:
            prod_table = doc.add_table(rows=len(production_lines) + 1, cols=6)
            prod_table.style = 'Light Grid Accent 1'

            # 表头
            headers = ['No.', 'Type', 'Dimensions (m)', 'Capacity (ton)', 'Crane No.', 'Crane (Type / Capacity t)']
            for j, h in enumerate(headers):
                safe_set_cell_text(prod_table.rows[0].cells[j], h)
                prod_table.rows[0].cells[j].paragraphs[0].runs[0].font.bold = True
                prod_table.rows[0].cells[j].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

            # 数据行
            for i, line in enumerate(production_lines, 1):
                if isinstance(line, dict):
                    line_no = safe_get_value(line, 'no', str(i))
                    line_type = safe_get_value(line, 'type', '')
                    dimensions = safe_get_value(line, 'dimensions')
                    capacity = safe_get_value(line, 'capacity')
                    cranes = line.get('cranes')
                    if isinstance(cranes, list) and cranes:
                        crane_no = len(cranes)
                        crane_infos = []
                        for c in cranes:
                            ct = safe_get_value(c, 'type', '')
                            cc = safe_get_value(c, 'capacity', '')
                            crane_infos.append(f"{ct}({cc}t)")
                        crane_info = '; '.join(crane_infos)
                    elif 'crane' in line:
                        old_crane = line.get('crane')
                        if isinstance(old_crane, dict):
                            crane_no = safe_get_value(old_crane, 'no', '')
                            crane_type = safe_get_value(old_crane, 'type', '')
                            crane_cap = safe_get_value(old_crane, 'capacity', '')
                            crane_info = f"{crane_type}({crane_cap}t)" if crane_type or crane_cap else 'N/A'
                        else:
                            crane_no = 'N/A'
                            crane_info = 'N/A'
                    else:
                        crane_no = 'N/A'
                        crane_info = 'N/A'

                    safe_set_cell_text(prod_table.rows[i].cells[0], line_no)
                    safe_set_cell_text(prod_table.rows[i].cells[1], line_type)
                    safe_set_cell_text(prod_table.rows[i].cells[2], dimensions)
                    safe_set_cell_text(prod_table.rows[i].cells[3], str(capacity))
                    safe_set_cell_text(prod_table.rows[i].cells[4], str(crane_no))
                    safe_set_cell_text(prod_table.rows[i].cells[5], crane_info)
                else:
                    for j in range(6):
                        safe_set_cell_text(prod_table.rows[i].cells[j], 'N/A' if j == 0 else str(i))
        else:
            doc.add_paragraph('No production line data available.')
    else:
        doc.add_paragraph('No production line data available.')

    doc.add_paragraph()

    # 3.3 配套设施
    doc.add_heading('3.3 Supporting Facilities', level=2)

    facilities_list = [
        ('Crane Parameters', safe_get_value(shipyard_data, 'crane_parameters')),
        ('Segmented Manufacturing Base', safe_get_value(shipyard_data, 'segmented_manufacturing_base')),
        ('Painting Workshop', safe_get_value(shipyard_data, 'painting_workshop')),
        ('Fitting-out Dock', safe_get_value(shipyard_data, 'fitting_out_dock')),
        ('Other Facilities', safe_get_value(shipyard_data, 'other_facilities'))
    ]

    for label, value in facilities_list:
        if value and value != 'N/A':
            para = doc.add_paragraph(style='List Bullet')
            para.add_run(f"{label}: ").bold = True
            para.add_run(value)

    doc.add_paragraph()

    # 3.4 产能
    doc.add_heading('3.4 Production Capacity', level=2)

    annual_vessel, annual_dwt = _format_annual_capacity(shipyard_data.get('annual_production_capacity'))
    capacity_para = doc.add_paragraph()
    capacity_para.add_run('Annual Production Capacity: ').bold = True
    if annual_vessel or annual_dwt:
        parts = []
        if annual_vessel:
            parts.append(f'Vessels: {annual_vessel}')
        if annual_dwt:
            parts.append(f'DWT: {annual_dwt:,}')
        capacity_para.add_run('; '.join(parts))
    else:
        capacity_para.add_run('N/A')

    doc.add_paragraph()

    # ===== 5. 核心产品 =====
    doc.add_heading('4. Core Products and Specialties', level=1)

    core_ship_types = shipyard_data.get('core_ship_types', '')
    if core_ship_types and core_ship_types != 'null' and core_ship_types != 'N/A':
        doc.add_heading('4.1 Core Ship Types', level=2)

        ship_types = [t.strip() for t in core_ship_types.split(';') if t.strip()]
        for ship_type in ship_types:
            doc.add_paragraph(ship_type, style='List Bullet')

        doc.add_paragraph()

    specialties = shipyard_data.get('specialties', '')
    if specialties and specialties != 'null' and specialties != 'N/A':
        doc.add_heading('4.2 Technical Specialties', level=2)
        spec_para = doc.add_paragraph(specialties)
        spec_para.paragraph_format.line_spacing = 1.5

    doc.add_paragraph()

    # ===== 6. 资质认证 =====
    doc.add_heading('5. Qualifications and Certifications', level=1)

    certifications = shipyard_data.get('qualification_certifications', '')
    if certifications and certifications != 'null' and certifications != 'N/A':
        cert_list = [c.strip() for c in certifications.split(';') if c.strip()]
        for cert in cert_list:
            doc.add_paragraph(cert, style='List Bullet')
    else:
        doc.add_paragraph('Qualification certifications information not available.')

    doc.add_paragraph()

    guarantee_capacity = safe_get_value(shipyard_data, 'letter_of_guarantee_capacity')
    if guarantee_capacity and guarantee_capacity != 'N/A':
        para = doc.add_paragraph()
        para.add_run('Letter of Guarantee Capacity: ').bold = True
        para.add_run(guarantee_capacity)

    doc.add_paragraph()

    # ===== 7. 订单信息 =====
    doc.add_heading('6. Orders Information', level=1)

    doc.add_heading('6.1 Current Orders', level=2)
    doc.add_paragraph('Current orders are retrieved from the database and updated based on new shipbuilding orders and vessel delivery news.')

    doc.add_heading('6.2 Historical Orders', level=2)
    doc.add_paragraph('Historical orders are retrieved from the database and corrected based on historical data and vessel deliveries.')

    doc.add_paragraph()

    # ===== 页脚 =====
    section = doc.sections[0]
    footer = section.footer
    footer_para = footer.paragraphs[0]
    footer_para.text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Confidential Document"
    footer_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_para.runs[0].font.size = Pt(8)
    footer_para.runs[0].font.italic = True
    footer_para.runs[0].font.color.rgb = RGBColor(128, 128, 128)

    # 保存文档
    try:
        doc.save(output_path)
        print(f"✅ Word文档已生成: {output_path}")
        return True
    except Exception as e:
        print(f"❌ 文档保存失败: {e}")
        return False



if __name__ == "__main__":
    # Simple self-test with minimal record
    test_data = {
        "English_name": "Sample Shipyard",
        "Local_name": "示例船厂",
        "location": "Shanghai, China",
        "country": "China",
        "is_state_owned": "N",
        "is_active": "Y",
        "shipyard_type": "build",
        "ubo": "Sample Group",
    }
    create_word_document(test_data, "/tmp/test_shipyard_profile.docx")
