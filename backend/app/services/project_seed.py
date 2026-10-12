"""
项目主数据种子：来自项目表格的有效列。
列：PMP号 | 项目名称 | 阶段 | 优先级 | 类型 | 船东/买方 | 船厂/卖方 | 负责人 | 跟进人
"""
from __future__ import annotations

# (pmp_no, name, stage, priority, project_type, shipowner, shipyard, manager, follow_up)
PROJECT_SEED: list[tuple[str, str, str, str, str, str, str, str, str]] = [
    ("PMP0000000101", "4*LR1 tanker NB project", "Contracting & Effective", "高", "New Building", "Braskem", "GSI", "Wilson YANG", "程沈熠"),
    ("PMP0000000102", "2*LR2 tanker NB project", "Completion", "高", "New Building", "Cape Shipping", "SWS", "Wilson YANG", "龙吟"),
    ("PMP0000000103", "2*MR tanker finance project", "Completion", "中", "Finance", "L&B", "Wuhu", "Wilson YANG", "龙吟"),
    ("PMP0000000901", "2*Suezmax Crude Tanker project", "Completion", "高", "New Building", "Cape Shipping", "SWS", "Wilson YANG", "龙吟"),
    ("PMP0000000902", "1+1*VLCC NB project", "Contracting & Effective", "高", "New Building", "Cape Shipping", "QBS", "Wilson YANG", "程沈熠"),
    ("PMP0000000904", "3*181k BC NB project", "Contracting & Effective", "高", "New Building", "Cape Shipping/Alcyon", "Hengli", "Wilson YANG", "龙吟"),
    ("PMP0000000905", "1*LPG+1*LEG finance project", "LOI Stage", "高", "Finance", "Braskem", "SOE", "Wilson YANG", "程沈熠"),
    ("PMP0000000908", "1*20k LNG bunker NB project", "Contracting & Effective", "高", "New Building", "China Bunker", "DSOC", "Wilson YANG", "龙吟"),
    ("PMP0000000909", "2+2*41000dwt Product Tanker project", "Contracting & Effective", "高", "New Building", "Super Eco", "Yangzhou Guoyu", "Wilson YANG", "龙吟"),
    ("PMP0000000910", "2+3*Cruise vessel project", "Origination and Development", "中", "New Building", "Nor Cruise", "SWS", "Wilson YANG", "龙吟"),
    ("PMP0000001000", "4+2*6800dwt MPP NB project", "Indication and Offer", "高", "New Building", "L&B", "Zhenjiang Shipyard", "Wilson YANG", "沈金金"),
    ("PMP0000001001", "2*6800dwt MPP NB project", "Indication and Offer", "高", "New Building", "L&B", "XYF shipyard", "Wilson YANG", "龙吟"),
    ("PMP0000001002", "2*4700dwt Chemical tanker project", "Indication and Offer", "高", "Agency", "Posidonia", "Inace", "Wilson YANG", "程沈熠"),
    ("PMP0000001003", "5*3600TEU container vessel project", "Indication and Offer", "高", "New Building", "Sino-America", "Chengxi Shipyard", "Wilson YANG", "程沈熠"),
    ("PMP0000001004", "1*26000dwt Chemical tanker NB project", "Suspended", "中", "New Building", "Mediterranea di Navigazione", "New Jiangzhou shipyard", "Wilson YANG", "龙吟"),
    ("PMP0000001005", "2*25000dwt MPP vessel finance project", "Suspended", "低", "Finance", "Euro-Africa", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001006", "2+2*64k Bulk Carrier finance project", "Origination and Development", "中", "Finance", "German owner", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001007", "1 Water Injection Dredger project", "Origination and Development", "极高", "Agency", "Posidonia", "Brazil yard", "Wilson YANG", "程沈熠"),
    ("PMP0000001009", "2*210k BC finance project", "Origination and Development", "极高", "Finance", "Cape Shipping", "Dajin Offshore", "Wilson YANG", "程沈熠"),
    ("PMP0000001010", "320k BC NB project charter to Winning", "Origination and Development", "中", "New Building", "Cape Shipping", "Hengli", "Wilson YANG", "程沈熠"),
    ("PMP0000001012", "1*26000dwt Chemical tanker finance project", "Suspended", "低", "Finance", "Mediterranea di Navigazione S.p.A.", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001013", "2*MR1 NB project", "Suspended", "极高", "New Building", "Augusta Due", "Anhui Zhongrun", "Wilson YANG", "程沈熠"),
    ("PMP0000001014", "Equipment Agency", "Origination and Development", "中", "Agency", "", "711", "Wilson YANG", "程沈熠"),
    ("PMP0000001015", "5*1900TEU NB project", "Origination and Development", "中", "New Building", "", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001016", "5*2800TEU NB project", "Origination and Development", "中", "New Building", "", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001017", "4800TEU resale project", "Suspended", "中", "SnP", "长顺", "GUOYU", "Wilson YANG", "程沈熠"),
    ("PMP0000001018", "1*20000dwt Chemical tanker NB project", "Origination and Development", "中", "New Building", "TB Marine", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001019", "1*20000cbm LNG Bunkering Vessel Project", "Origination and Development", "中", "SnP", "TB Marine", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001020", "82k finance project", "Origination and Development", "高", "Finance", "Posidonia", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001021", "MR finance project", "Origination and Development", "高", "Finance", "Posidonia", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001022", "10000 cbm LNG FSU Barge", "Suspended", "低", "New Building", "MOLGAS", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001023", "Equipment PAYS Fund project", "Origination and Development", "中", "investment", "", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001024", "10*3800dwt MPP Vessel", "Origination and Development", "极高", "New Building", "german owner", "tbn", "Wilson YANG", "程沈熠"),
    ("PMP0000001025", "1*1600TEU resale project", "Suspended", "极高", "SnP", "Virono", "Fujian Shangde", "Wilson YANG", "程沈熠"),
    ("PMP0000001026", "2*Suezmax NB project", "Suspended", "极高", "New Building", "Liquimar", "SWS", "Wilson YANG", "龙吟"),
    ("PMP0000001027", "2*MR2 resale project", "Origination and Development", "极高", "SnP", "Hercules", "season/cssc leasing", "Wilson YANG", "程沈熠"),
    ("PMP0000001028", "3*1056TEU resale project", "Origination and Development", "极高", "SnP", "Virono", "hechuang shipyard", "Wilson YANG", "程沈熠"),
    ("PMP0000001029", "2*Suezmax NB project", "Suspended", "低", "New Building", "SH Group", "", "Wilson YANG", "程沈熠"),
    ("PMP0000001030", "1200TEU NB project", "Origination and Development", "中", "New Building", "", "Guangji shipyard", "Wilson YANG", "程沈熠"),
    ("PMP0000001031", "7500cbm LNG bunker", "Origination and Development", "高", "New Building", "TB Marine", "DSOC", "Wilson YANG", "程沈熠"),
]
