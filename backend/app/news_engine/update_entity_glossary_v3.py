# -*- coding: utf-8 -*-
"""
第三轮补全：针对剩余 133 条 entity 的官方/合理英文名称。
来源：联网核查（交易所公告、公司官网、克拉克森等）+ 行业通用译法。
"""
import json
import os
import sqlite3

# 统一使用 news_engine/data 下的主词表库（有 287 条 shipyard 记录），
# 不再使用 detectors/data 下的空库副本。
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data',
    'entity_registry.db'
)

FINAL_ENGLISH_MAP = {
    # buyer / seller
    "未知买方": "Unknown Buyer",
    "招商轮船": "China Merchants Energy Shipping (CMES)",

    # company
    "永联公司": "Yonglian Company",
    "蓬莱京鲁船业": "Penglai Jinglu Shipyard",
    "扬子江船业（控股）有限公司": "Yangzijiang Shipbuilding (Holdings) Ltd.",
    "江南造船（集团）有限责任公司": "Jiangnan Shipyard (Group) Co., Ltd.",
    "鼎衡集团": "Dingheng Group",
    "帕多瓦内陆码头运营公司": "Padova Inland Terminal Operator",

    # shipowner
    "马士基集团": "A.P. Moller-Maersk Group",
    "中远海运控股股份有限公司": "COSCO Shipping Holdings Co., Ltd.",
    "英国蓝普船舶": "British Lamp Shipping",
    "新加坡船东": "Singapore Shipowner",
    "爱达邮轮": "Adora Cruises",
    "中交海峰风电": "CCCC Haifeng Wind Power",
    "正力海洋工程有限公司": "Zhengli Ocean Engineering Co., Ltd.",
    "戴安娜航运公司": "Diana Shipping Inc.",
    "苏美达海运有限公司": "SUMEC Marine Co., Ltd.",
    "以星航运有限公司": "ZIM Integrated Shipping Services Ltd.",
    "阿布扎比港口集团": "AD Ports Group",
    "中远海运散货运输有限公司": "COSCO Shipping Bulk Carrier Co., Ltd.",
    "金辉集团有限公司": "Jinhui Holdings Ltd.",
    "长锦商船": "Sinokor Merchant Marine",
    "新兴航运有限公司": "New Jinhai Shipping Co., Ltd.",
    "交通运输部救助打捞局": "Ministry of Transport Rescue and Salvage Bureau",
    "中交第二航务工程局有限公司": "CCCC Second Harbour Engineering Company Ltd.",
    "戴安娜航运股份有限公司": "Diana Shipping Inc.",
    "根科船务有限公司": "Genco Shipping & Trading Limited",
    "维多国际航运公司": "Vitol International Shipping",
    "东方海外货柜航运有限公司": "Orient Overseas Container Line Limited (OOCL)",
    "扬子江海事发展有限公司": "Yangzijiang Maritime Development Co., Ltd.",
    "洲际船务集团控股有限公司": "Seacon Shipping Group Holdings Limited",
    "美森轮船有限公司": "Matson Navigation Company, Inc.",
    "皇家加勒比国际游轮公司": "Royal Caribbean International",
    "托克集团": "Trafigura Group",
    "海丰国际控股有限公司": "SITC International Holdings Co., Ltd.",
    "哈斯基石油中国有限公司": "Husky Oil China Limited",
    "中租迪和股份有限公司": "Chailease International Finance Corporation",
    "扬子江船业集团": "Yangzijiang Shipbuilding Group",
    "商船三井": "Mitsui O.S.K. Lines (MOL)",
    "巴西国家石油公司": "Petróleo Brasileiro S.A. (Petrobras)",
    "达飞轮船": "CMA CGM",
    "希腊船东（泛指）": "Greek Shipowner",
    "日本船东（泛指）": "Japanese Shipowner",
    "印度尼西亚船东（泛指）": "Indonesian Shipowner",
    "越南船东（泛指）": "Vietnamese Shipowner",
    "土耳其船东（泛指）": "Turkish Shipowner",
    "招商局能源运输股份有限公司": "China Merchants Energy Shipping Co., Ltd.",
    "韩新海运": "HMM Co., Ltd.",
    "越南ASP集团": "Vietnam ASP Group",
    "汇舸环保科技集团股份有限公司": "Contiocean Environment Tech Group Co., Ltd.",
    "英国皇家海军": "Royal Navy",
    "英国皇家辅助舰队": "Royal Fleet Auxiliary",
    "东海安和": "EGPN Bulk Carrier",
    "金辉集团": "Jinhui Holdings",
    "万海航运股份有限公司": "Wan Hai Lines Ltd.",
    "浙港柒号航运有限公司": "Zhejiang Port No.7 Shipping Co., Ltd.",
    "佳盈海运有限公司": "Jiaying Shipping Co., Ltd.",
    "兴通海运股份有限公司": "Xingtong Shipping Co., Ltd.",
    "中远海运集装箱运输有限公司": "COSCO Shipping Lines Co., Ltd.",
    "中联航运有限公司": "China United Lines (CULines)",
    "卡塔尔能源公司": "QatarEnergy",
    "阿布扎比国家石油公司": "Abu Dhabi National Oil Company (ADNOC)",
    "中远海运特种运输股份有限公司": "COSCO Shipping Specialized Carriers Co., Ltd.",
    "现代重工": "Hyundai Heavy Industries",
    "裕民航运": "U-Ming Marine Transport",
    "锦江航运（通过满强航运下单）": "Jinjiang Shipping (via Manqiang Shipping)",
    "大津重工": "Dajin Heavy Industry",
    "樱花海洋（Sakura Ocean）": "Sakura Ocean",
    "斯岛之阳轮船东": "Shipowner of Sun of Scapa",
    "美国交通部海事管理局": "Maritime Administration (MARAD), U.S. DOT",
    "华润大东": "China Resources Dadong",
    "嘉年华": "Carnival Corporation",
    "沪东中华造船（集团）有限公司": "Hudong-Zhonghua Shipbuilding (Group) Co., Ltd.",
    "德国Hartmann公司": "Hartmann Reederei / Hartmann Group",
    "君正船务": "SC Shipping (Shanghai)",
    "用户": "User",
    "新加坡联合海事公司": "United Maritime Corporation (Singapore)",
    "益洋航运集团": "Yiyang Shipping Group",
    "布埃诺轮": "Bueno",
    "泛洋海运": "Pan Ocean Co., Ltd.",
    "亚洲某船东": "Asian Shipowner",
    "招商船舶": "China Merchants Shipbuilding",
    "福港拖1": "Fugang Tug 1",
    "欧洲某订造方": "European Ordering Party",
    "长顺": "Changshun",
    "立德信": "Lidexin",
    "太平洋气体船": "Pacific Gas",
    "大德轮": "Dade",
    "葛洲坝": "Gezhouba Group",
    "欧洲某船东": "European Shipowner",
    "德国Elbdeich公司": "Elbdeich Reederei",

    # shipyard
    "中船澄西船舶修造有限公司": "CSSC Chengxi Shipbuilding Co., Ltd.",
    "广船国际有限公司": "Guangzhou Shipyard International Co., Ltd.",
    "中船北海造船有限公司": "CSSC Beihai Shipbuilding Co., Ltd.",
    "海庭船舶科技有限公司": "Seatrium Engineering Technology (Hi-Ting)",
    "响水万隆重工有限公司": "Xiangshui Wanlong Heavy Industry Co., Ltd.",
    "华泰重工有限公司": "Huatai Heavy Industry Co., Ltd.",
    "中交四航局江门航通船业有限公司": "CCCC Fourth Harbour Engineering Jiangmen Hangtong Shipbuilding Co., Ltd.",
    "奥斯汀公司": "Austal Limited",
    "江龙船艇科技股份有限公司": "Jianglong Shipbuilding Technology Co., Ltd.",
    "荣成市华东造船有限公司": "Rongcheng Huadong Shipbuilding Co., Ltd.",
    "威立雅海洋环境工业（广东）有限公司": "Veolia Marine Environmental Industries (Guangdong) Co., Ltd.",
    "启东象屿海洋装备有限责任公司": "Qidong Xiangyu Marine Equipment Co., Ltd.",
    "英辉南方造船（广州）有限公司": "Yinghui Southern Shipbuilding (Guangzhou) Co., Ltd.",
    "迈尔船厂": "Meyer Werft",
    "文冲修造": "Wenchong Repair & Conversion",
    "中船广船国际有限公司": "CSSC Guangzhou Shipyard International Co., Ltd.",
    "恒力重工集团有限公司": "Hengli Heavy Industry Group Co., Ltd.",
    "西江（桂平）船舶修造产业园": "Xijiang (Guiping) Shipbuilding & Repair Industrial Park",
    "城东造船": "Chengdong Shipyard",
    "招商局工业集团有限公司（海门基地）": "China Merchants Industry Group Co., Ltd. (Haimen Base)",
    "启东乾耀重工": "Qidong Qianyao Heavy Industry",
    "中船广西造船有限公司": "CSSC Guangxi Shipbuilding Co., Ltd.",
    "沙特萨勒曼国王国际综合港务设施项目": "King Salman International Integrated Port Facilities Project",
    "万邦船舶重工（舟山）有限公司": "Wanbang Ship Heavy Industry (Zhoushan) Co., Ltd.",
    "西班牙Cardama船厂": "Cardama Shipyard (Spain)",
    "中船桂江造船有限公司": "CSSC Guijiang Shipbuilding Co., Ltd.",
    "三星重工越南造船基地": "Samsung Heavy Industries Vietnam Shipbuilding Base",
    "海西重机": "Haixi Heavy Machinery",
    "山船重工": "Shanhaiguan Shipbuilding Industry Co., Ltd.",
    "广济新能船业": "Guangji New Energy Shipbuilding",
    "万隆船舶重工有限公司": "Wanlong Ship Heavy Industry Co., Ltd.",
    "启东中远海运海洋工程有限公司": "Qidong COSCO Shipping Offshore Engineering Co., Ltd.",
    "奥斯本南区船厂": "Osborne South Shipyard",
    "华祥高端气体船舶基地": "Huaxiang High-end Gas Vessel Base",
    "显利造船": "Hin Lee Shipyard",
    "博迈科（Bomesc）": "Bomesc Offshore Engineering",
    "招商局重工（海门）有限公司": "China Merchants Heavy Industry (Haimen) Co., Ltd.",
    "博迈科海洋工程股份有限公司": "Bomesc Offshore Engineering Co., Ltd.",
    "尾道造船": "Onomichi Dockyard",
    "常石造船": "Tsuneishi Shipbuilding",
    "武昌造船": "Wuchang Shipbuilding",
    "招商局威海船厂": "China Merchants Weihai Shipyard",
}


def has_english_alias(aliases):
    for a in aliases:
        if not isinstance(a, str) or not a.strip():
            continue
        first = a.strip()[0]
        try:
            if first.encode('ascii').decode().isalpha():
                return True
        except Exception:
            continue
    return False


def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, entity_type, canonical_name, aliases FROM entities WHERE entity_type IN ('company','shipyard','shipowner','seller','buyer')")
    rows = c.fetchall()

    updated = 0
    still_missing = []

    for row_id, etype, name, aliases_str in rows:
        aliases = json.loads(aliases_str or '[]')
        if has_english_alias(aliases):
            continue

        en_name = FINAL_ENGLISH_MAP.get(name)
        if en_name:
            new_aliases = [en_name] + [a for a in aliases if a != en_name]
            c.execute('UPDATE entities SET aliases = ? WHERE id = ?', (json.dumps(new_aliases, ensure_ascii=False), row_id))
            updated += 1
        else:
            still_missing.append((etype, name))

    conn.commit()
    conn.close()

    print(f"最终补全: {updated}")
    print(f"仍缺英文: {len(still_missing)}")
    for et, name in still_missing:
        print(f"  [{et}] {name}")


if __name__ == "__main__":
    main()
