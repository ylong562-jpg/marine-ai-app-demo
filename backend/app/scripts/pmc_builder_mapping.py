# -*- coding: utf-8 -*-
"""
PMC builder -> shipyard_data 绑定映射（AI 人工比对结果，2026-09-23）

来源：
- auto_exact  : builder 与 shipyard_data.English_name/Abreviation/former_name 归一化后完全一致
- auto_core   : 去除通用词后核心名归一化一致
- ai_review   : 人工逐条审核候选匹配（分数 0.5+ 区间）
- ai_override : 自动候选错误，人工指定正确船厂

未收录的 builder = 船厂数据库中无对应船厂，按需求暂不处理。
"""

AUTO_EXACT = {
    "Austal Vietnam": 269,
    "CMHI (Jiangsu)": 54,
    "CMHI (Shenzhen)": 53,
    "DH Shipbuilding": 273,
    "Estaleiro Rio Grande": 275,
    "Gelibolu Shipyard": 538,
    "Haidong Shipyard": 189,
    "Incat Tasmania": 466,
    "Marco Polo Shipyard": 409,
    "Meyer Werft": 374,
    "Namura Shipbuilding": 463,
    "Nihon Shipyard": 455,
    "Onomichi Dockyard": 316,
    "Shimanami Shipyard": 407,
    "SK Oceanplant": 581,
    "Tersan Shipyard": 279,
    "VARD Vung Tau": 505,
}

AUTO_CORE = {
    "Bollinger Marine": 271,
    "Chengxi Shipyard": 56,
    "Cochin Shipyard": 395,
    "CSSC Tianjin": 101,
    "Dalian Liaonan": 408,
    "Dalian Shipbuilding": 62,
    "Damen Yichang": 186,
    "Fincantieri Ancona": 464,
    "Fincantieri Marghera": 526,
    "Hunan Xiangchuan": 518,
    "Jiangmen Nanyang": 345,
    "Jiangsu Jiuzhou": 193,
    "Jiangsu Qinfeng": 194,
    "Jiangsu Soho Marine": 49,
    "Jingjiang Nanyang": 255,
    "Lianyungang Wuzhou": 434,
    "Nantong Yahua": 195,
    "Oshima Shipbuilding": 496,
    "PaxOcean Zhoushan": 162,
    "Qidong Jisheng": 232,
    "Shanghai Shipyard": 124,
    "Shanghai Waigaoqiao": 42,
    "Swan Defence": 476,
    "Taizhou Haibin": 236,
    "Taizhou Maple Leaf": 170,
    "Tsuneishi Zhoushan": 128,
    "Wuchang Shipbuilding": 129,
    "Wuhu Shipyard": 43,
    "Xiamen Shipbuilding": 132,
    "Yangzhou Guoyu": 256,
    "Yangzhou Nakanishi": 163,
    "Zhejiang Donghong": 353,
    "Zhejiang Friendship": 160,
    "Zhejiang Hongsheng": 245,
    "Zhejiang Kailing": 201,
    "Zhejiang Tenglong": 183,
    "Zhejiang Tianshi": 246,
    "Zhejiang Yongxin": 489,
    "Zhoushan Changhong": 150,
    "Zhoushan Dashenzhou": 103,
    "Zhuhai Jianglong": 110,
}

# 人工审核通过的候选匹配（builder -> shipyard_data.id）
AI_REVIEW = {
    "Xin Yue Feng SY": 90,
    "Jiangsu New Hantong": 111,
    "Nantong COSCO KHI": 74,
    "Dalian COSCO KHI": 63,
    "Jiangsu Zhenjiang SY": 342,
    "Yangzhou Universe SB": 239,
    "Fujian Southeast SB": 65,
    "Jiangxi Jiangxin SB": 161,
    "Shandong Xinneng SB": 175,
    "Taizhou Jianxing HI": 179,
    "Zhejiang Mingfei SB": 115,
    "Jiangsu Haifeng SY": 340,
    "Jiangsu Yixiang SB": 501,
    "Ningbo Penghong SB": 392,
    "Chizhou Tianyu SB": 127,
    "Hubei Hechuang HI": 51,
    "New Xiangsheng SB": 226,
    "Qidong Qianyao HI": 305,
    "Weihai Wuchuan SB": 398,
    "Yangzhou Ryuwa SB": 198,
    "CSSC Guijiang SY": 292,
    "Fujian Funing SB": 171,
    "Hengli SB (Dalian)": 106,
    "Hunan Jinhang SB": 181,
    "Jiangsu Dajin HI": 102,
    "Ningbo Zhenhe SB": 196,
    "Royal Bodewes SY": 575,
    "Taizhou Kouan SB": 238,
    "CSSC Guangxi SB": 291,
    "Fujian Mawei SB": 147,
    "Linhai Huipu SB": 219,
    "Ningbo Xinle SB": 83,
    "Ningbo Boda SB": 227,
    "South China SY": 80,
    "Ferus Smit SY": 576,
    "New Dayang SB": 75,
    "Huangpu Wenchong": 59,
    "Yantai CIMC Raffles": 119,
    "New Times SB": 76,
    "Panjin Dajin Off": 517,
    "Taizhou Sanfu": 81,
    "Niestern Sander": 430,
    "Ferus Smit Leer": 576,
    "Zhejiang Shenzhou": 257,
    "Yangzi-Mitsui SB": 135,
    "Chantiers Atlantique": 483,
    "Hanwha Ocean": 500,
    "Naikai Zosen Setoda": 446,
    "Nantong Xiangyu SOE": 82,
    "Shanhaiguan SB": 123,
    "Tangshan Dajin Off": 388,
    "Balenciaga SY": 321,
    "Dayang Offshore": 64,
    "Ningde Jiangang": 229,
    "COSCO HI (Guangdong)": 167,
    "Shandong Fuerdun": 91,
    "Fujian Huahai": 422,
    "COSCO HI (Yangzhou)": 138,
    "COSCO HI (Zhoushan)": 100,
    "Jiangsu New YZJ": 151,
    "Qidong Xiangyu": 306,
    "Huanghai SB": 109,
    "Jiangnan SY Group": 61,
    "Ocean Crown Tech": 197,
    "COSCO HI (Dalian)": 98,
    "Yangzi Hongyuan SB": 503,
    "Imabari SB (Imabari)": 295,
    "Samsung HI": 283,
    "Jiangsu Sentosa": 215,
    "Anhui Zhongrun HI": 472,
    "Zhoushan Ningshing": 471,
    "Ferguson Marine Eng": 530,
    "Yangzi Xinfu SB": 136,
    "Zhejiang Zhenxing": 356,
    "Qingdao Beihai SB": 48,
    "Armon (Vigo)": 423,
    "Jiangsu Huatai SB": 192,
    "Fincantieri Sestri": 368,
    "Hanwha Philly SY": 500,
    "Tsuneishi Zosen": 128,
    "Fincantieri Palermo": 368,
    "Nantong Tongsheng": 225,
    "Haitong Offshore Eng": 164,
    "Yizheng Kangping": 447,
    "Fincantieri Monfalco": 368,
    "Watts Energy": 438,
    "New Jiangzhou SB": 344,
    "Yichang Xinhui SB": 445,
    "Zhoushan Hetai SB": 495,
    "COSCO HI (Qidong)": 99,
    "Zhejiang Xinxinzhou": 166,
    "Fincantieri Bay SB": 368,
    "Liaoning Xinhai SB": 250,
    "Penglai Jinglu SY": 78,
    "Huajie Shipbuilding": 218,
    "Chowgule (Mangalore)": 580,
    "Hudong Zhonghua": 60,
    "Garden Reach SB": 389,
    "CSBC (Kaohsiung)": 431,
    "Ningbo Dongsheng": 228,
    "JMU Tsu Imabari": 295,
    "Meyer Turku": 374,
    "Austal Philippines": 314,
    "Imabari SB Saijo": 295,
    "JMU Kure Imabari": 295,
    "Zhongtian HI": 479,
    "Kawasaki HI Sakaide": 320,
    "JMU Isogo Imabari": 295,
    "Ulstein Ulsteinvik": 497,
    "Chowgule SB Loutulim": 580,
    "SOHO I&T SB": 49,
    "PaxOcean Graha": 230,
    "Tri Waters Anhui": 173,
    "Imabari SB Marugame": 295,
    "Imabari SB Hiroshima": 295,
    "Ningbo Guangxin SB": 66,
    "Armon (Navia)": 423,
    "Haixin HI": 212,
    "Wison New Energies": 299,
    "Royal T Kampen": 575,
    "Huarun Dadong": 107,
    "Lixin Ship": 494,
    "Armon (Gijon)": 423,
    "Naikai Innoshima": 446,
    "HD Hyundai HI (HHI)": 276,
    "Siying Tongshun Yard": 214,
    "Armon (Burela)": 423,
    "Nakanoshima Shpbdg": 163,
    "Fincantieri": 368,
    "HD Hyundai Samho": 276,
}

# 自动候选错误、人工指定的正确匹配
AI_OVERRIDE = {
    "Nantong CIMC SOE": 52,        # 自动候选误配惠生南通
    "CMJL (Nanjing)": 71,          # 招商局金陵(南京)->金陵船舶
    "Yuexin Engineering": 137,     # 粤新->广东粤新海洋工程
    "Hexing Shipyard": 68,         # 合兴->浙江合兴船厂
    "Jiangmen Hangtong": 348,      # 江门航通
    "Kawasaki HI Kobe": 320,       # 川崎神户工场
    "QB Qingdao Wuchuan": 515,     # 青岛武船麦克德莫特
    "Balaena Birkenhead": 270,
    "Hantong WinG HI": 67,         # 韩通 WinG 机型船厂
    "Guangxin Equipment": 66,      # 广新海事重工
    "HD Hyundai (Medium)": 362,    # 现代尾浦(中型船)
    "GSI Nansha": 58,              # 广船国际南沙->广州广船国际
    # 2026-09-23 二次人工审核补充（缩写型 builder，首次匹配未命中）
    "CMI (Qingdao)": 79,           # 用户已删除 405(招商局青岛船厂)条目，订单归并到青岛造船厂有限公司
    "CMI (Weihai)": 149,           # 招商局金陵船舶（威海）CMJW
    "COOEC Qingdao": 289,          # 海油工程(COOEC)青岛场地
    "ZPMC Qidong Marine": 141,     # 振华重工(ZPMC)启东海洋基地 -> 集团船厂条目141（原287已并入141）
    "Tsuneishi Cebu": 128,         # 常石造船(宿务船厂->日本母公司条目)（原323已并入128）
    "Yizheng Yangzi SB": 329,      # 扬州仪征相关船厂（库内专用条目）
    "Xiangshui Wanlong HI": 182,   # 万隆船舶重工（原281已并入兖州万隆182；PMC英文名归一为Yanzhou Wanlong）
    "Nantong Rainbow": 165,        # 南通润邦(Rainbow/润邦)
    "Wuhu (Nantong)": 43,          # 芜湖造船厂南通基地（同一法人）
}

BUILDER_MAP = {}
for _src, _d in (("auto_exact", AUTO_EXACT), ("auto_core", AUTO_CORE),
                 ("ai_review", AI_REVIEW), ("ai_override", AI_OVERRIDE)):
    for _b, _yid in _d.items():
        BUILDER_MAP[_b] = {"shipyard_id": _yid, "match_type": _src}
