"""

将船厂数据导出为Excel格式（严格按照模板格式）
"""
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import os
def _format_value(value):
    """辅助函数：格式化数据库值为可显示的字符串"""
    if value is None:
        return ""
    if isinstance(value, str) and value.lower() in ('null', 'none'):
        return ""
    return value


def _format_annual_capacity(value):
    """辅助函数：将年产能 JSON 拆分为 vessel/specs 两个可显示字符串"""
    if not value:
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return value, None
    if isinstance(value, dict):
        vessel = value.get('vessel')
        specs = value.get('specs')
        raw = value.get('_raw')
        return vessel, specs
    return str(value), None


def _format_date(value):
    """辅助函数：格式化日期"""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, str):
        try:
            dt = datetime.strptime(value, '%Y-%m-%d')
            return dt.strftime('%Y-%m-%d')
        except Exception:
            pass
        try:
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%Y-%m-%d')
        except Exception:
            return value
    return str(value)


def _parse_production_lines(value):
    """解析 production_lines 为列表"""
    if not value:
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    if isinstance(value, list):
        return value
    return []


def create_excel_with_all_shipyards(shipyards_data: List[Dict[str, Any]], output_path: str):
    """
    将所有船厂数据导出到同一个Excel表格中。
    production_lines 按条目展开为多行，基本信息列垂直合并，参考 shipyard schema.xlsx 的分行方式。

    Excel列结构（32列）:
    A: English_name
    B: Local_name
    C: Abreviation
    D: location
    E: establishment_date
    F: country
    G: state_owned
    H: is_active (Y/N)
    I: shipyard_type (build/repair/build/repair)
    J: group
    K: ubo
    L: qualification_certifications
    M: refund_guarantee_capacity
    N: annual_vessel (年造船数)
    O: annual_dwt (年生产总吨 DWT)
    P: shipyard_area
    Q: Quayside
    R: No. (production_lines)
    S: Type (production_lines: slipway/dry dock)
    T: Capacity (production_lines, ton)
    U: Dimensions (production_lines, m)
    V: Crane No.
    W: Crane Type (gantry crane/normal crane)
    X: Crane Capacity (t)
    Y: crane_parameters
    Z: Block_Assembly_Workshop
    AA: painting_workshop
    AB: Outfitting_workshop
    AC: other_facilities
    AD: main_ship_types_now
    AE: ship_types_delivered
    AF: update_time
    """

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Shipyard_Information"

    column_widths = {
        'A': 30, 'B': 30, 'C': 15, 'D': 25, 'E': 18,
        'F': 12, 'G': 12, 'H': 12, 'I': 15, 'J': 20, 'K': 25,
        'L': 30, 'M': 25, 'N': 15, 'O': 18, 'P': 15, 'Q': 15, 'R': 10,
        'S': 15, 'T': 15, 'U': 25, 'V': 12, 'W': 18, 'X': 16, 'Y': 35,
        'Z': 25, 'AA': 20, 'AB': 20, 'AC': 25, 'AD': 40, 'AE': 35, 'AF': 18
    }
    for col, width in column_widths.items():
        ws.column_dimensions[col].width = width

    header_font = Font(name='Arial', size=11, bold=True, color='000000')
    header_fill = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    data_font = Font(name='Arial', size=10)
    data_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    thin_border = Border(
        left=Side(style='thin', color='000000'),
        right=Side(style='thin', color='000000'),
        top=Side(style='thin', color='000000'),
        bottom=Side(style='thin', color='000000')
    )

    fill_even = PatternFill(start_color='F2F2F2', end_color='F2F2F2', fill_type='solid')
    fill_odd = PatternFill(start_color='FFFFFF', end_color='FFFFFF', fill_type='solid')

    headers = [
        'English_name', 'Local_name', 'Abreviation',
        'location', 'establishment_date', 'country', 'state_owned',
        'is_active', 'shipyard_type',
        'group', 'ubo', 'qualification_certifications', 'refund_guarantee_capacity',
        'annual_vessel', 'annual_dwt', 'shipyard_area', 'Quayside',
        'No.', 'Type', 'Capacity', 'Dimensions',
        'Crane No.', 'Crane Type', 'Crane Capacity',
        'crane_parameters', 'Block_Assembly_Workshop', 'painting_workshop', 'Outfitting_workshop',
        'other_facilities', 'main_ship_types_now', 'ship_types_delivered',
        'update_time'
    ]

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = 'A2'

    current_row = 2
    for idx, shipyard_data in enumerate(shipyards_data):
        production_lines = _parse_production_lines(shipyard_data.get('production_lines'))
        line_count = len(production_lines) if production_lines else 1
        start_row = current_row
        end_row = current_row + line_count - 1
        bg_fill = fill_even if idx % 2 == 0 else fill_odd

        # 基本信息（仅在起始行写入）
        ws.cell(row=start_row, column=1, value=_format_value(shipyard_data.get('English_name', shipyard_data.get('name', ''))))
        ws.cell(row=start_row, column=2, value=_format_value(shipyard_data.get('Local_name', '')))
        ws.cell(row=start_row, column=3, value=_format_value(shipyard_data.get('Abreviation', shipyard_data.get('abbreviation', ''))))
        ws.cell(row=start_row, column=4, value=_format_value(shipyard_data.get('location', '')))
        est_date = _format_date(shipyard_data.get('establishment_date', ''))
        ws.cell(row=start_row, column=5, value=est_date)
        if est_date:
            ws.cell(row=start_row, column=5).number_format = 'yyyy-mm-dd'
        ws.cell(row=start_row, column=6, value=_format_value(shipyard_data.get('country', '')))
        ws.cell(row=start_row, column=7, value=_format_value(shipyard_data.get('state_owned', shipyard_data.get('is_state_owned', ''))))
        ws.cell(row=start_row, column=8, value=_format_value(shipyard_data.get('is_active', '')))
        ws.cell(row=start_row, column=9, value=_format_value(shipyard_data.get('shipyard_type', '')))
        ws.cell(row=start_row, column=10, value=_format_value(shipyard_data.get('group', shipyard_data.get('group_name', ''))))
        ws.cell(row=start_row, column=11, value=_format_value(shipyard_data.get('ubo', '')))
        ws.cell(row=start_row, column=12, value=_format_value(shipyard_data.get('qualification_certifications', '')))
        ws.cell(row=start_row, column=13, value=_format_value(shipyard_data.get('refund_guarantee_capacity', shipyard_data.get('letter_of_guarantee_capacity', ''))))
        annual_vessel, annual_dwt = _format_annual_capacity(shipyard_data.get('annual_production_capacity', ''))
        ws.cell(row=start_row, column=14, value=_format_value(annual_vessel))
        ws.cell(row=start_row, column=15, value=_format_value(annual_dwt))
        ws.cell(row=start_row, column=16, value=_format_value(shipyard_data.get('shipyard_area', '')))
        ws.cell(row=start_row, column=17, value=_format_value(shipyard_data.get('Quayside', shipyard_data.get('total_shoreline_length', ''))))

        # crane_parameters 及后面的基本信息
        crane_params = shipyard_data.get('crane_parameters', '')
        if isinstance(crane_params, (list, dict)):
            crane_params = json.dumps(crane_params, ensure_ascii=False)
        ws.cell(row=start_row, column=25, value=_format_value(crane_params))
        ws.cell(row=start_row, column=26, value=_format_value(shipyard_data.get('Block_Assembly_Workshop', shipyard_data.get('segmented_manufacturing_base', ''))))
        ws.cell(row=start_row, column=27, value=_format_value(shipyard_data.get('painting_workshop', '')))
        ws.cell(row=start_row, column=28, value=_format_value(shipyard_data.get('Outfitting_workshop', shipyard_data.get('fitting_out_dock', ''))))
        ws.cell(row=start_row, column=29, value=_format_value(shipyard_data.get('other_facilities', '')))
        main_types = shipyard_data.get('main_ship_types_now', '')
        if not main_types or (isinstance(main_types, str) and main_types.lower() in ('null', 'none')):
            main_types = shipyard_data.get('core_ship_types', '')
        ws.cell(row=start_row, column=30, value=_format_value(main_types))
        ws.cell(row=start_row, column=31, value=_format_value(shipyard_data.get('ship_types_delivered', '')))
        update_time = _format_date(shipyard_data.get('update_time', ''))
        ws.cell(row=start_row, column=32, value=update_time)
        if update_time:
            ws.cell(row=start_row, column=32).number_format = 'yyyy-mm-dd'

        # production_lines 展开到 R~X (18~24)
        for i, line in enumerate(production_lines):
            row_num = start_row + i
            ws.cell(row=row_num, column=18, value=line.get('no', ''))
            ws.cell(row=row_num, column=19, value=_format_value(line.get('type', '')))
            ws.cell(row=row_num, column=20, value=_format_value(line.get('capacity', '')))
            ws.cell(row=row_num, column=21, value=_format_value(line.get('dimensions', '')))
            cranes = line.get('cranes')
            if isinstance(cranes, list) and cranes:
                # 将多台吊机信息合并为一个字符串显示
                crane_texts = []
                for c in cranes:
                    ctype = c.get('type', '')
                    ccap = c.get('capacity', '')
                    crane_texts.append(f"{ctype}({ccap}t)")
                ws.cell(row=row_num, column=22, value=len(cranes))
                ws.cell(row=row_num, column=23, value=_format_value(cranes[0].get('type', '') if cranes else ''))
                ws.cell(row=row_num, column=24, value='; '.join(crane_texts))
            elif 'crane' in line:
                # 兼容旧格式
                old_crane = line.get('crane')
                if isinstance(old_crane, dict):
                    ws.cell(row=row_num, column=22, value=old_crane.get('no', ''))
                    ws.cell(row=row_num, column=23, value=_format_value(old_crane.get('type', '')))
                    ws.cell(row=row_num, column=24, value=_format_value(old_crane.get('capacity', '')))

        # 垂直合并基本信息列（除产线展开列 Q~W 外）
        if line_count > 1:
            merge_cols = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 25, 26, 27, 28, 29, 30, 31, 32]
            for col in merge_cols:
                ws.merge_cells(start_row=start_row, start_column=col, end_row=end_row, end_column=col)

        # 应用样式
        for row in range(start_row, end_row + 1):
            ws.row_dimensions[row].height = 20
            for col in range(1, 33):  # A-AF
                cell = ws.cell(row=row, column=col)
                cell.font = data_font
                cell.alignment = data_alignment
                cell.border = thin_border
                cell.fill = bg_fill

        current_row = end_row + 1

    wb.save(output_path)
    print(f"✓ Excel文件已生成: {output_path}")


def create_excel_from_template(shipyard_data: Dict[str, Any], output_path: str):
    """
    根据模板创建单个船厂的Excel文件（兼容旧接口）
    """
    create_excel_with_all_shipyards([shipyard_data], output_path)


if __name__ == "__main__":
    # Simple self-test with empty record
    create_excel_with_all_shipyards([], "/tmp/test_shipyard_export.xlsx")
