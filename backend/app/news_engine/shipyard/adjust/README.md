# 产线数据再生成工具 (Production Lines Regenerator)

## 功能说明

本工具用于将现有数据库中的船厂 `production_lines` 数据按照新规范重新生成。

### 核心任务
1. 读取数据库中每个船厂的现有 `production_lines` 和 `crane_parameters`
2. 调用 AI 按照新规范重新生成 `production_lines`
3. 覆盖旧的 `production_lines`，**保留 `crane_parameters` 不变**

### 新数据规范

`production_lines` 中每条产线现在包含以下字段：

```json
{
  "no": 1,
  "type": "dry dock",
  "capacity": 30000,
  "dimensions": "530 × 130 × 13.8",
  "crane": {
    "no": 1,
    "type": "gantry crane",
    "capacity": 900
  }
}
```

**字段说明：**
- `no`: 产线编号（整数）
- `type`: 产线类型，**严格限定**为 `"slipway"` 或 `"dry dock"`
- `capacity`: 载重能力，**纯数字**，单位默认为 **ton**
- `dimensions`: 尺寸，**纯数字**用 `" × "` 分隔，单位默认为 **m**
- `crane`: 吊机信息
  - `no`: 吊机编号（整数）
  - `type`: 吊机类型，**严格限定**为 `"gantry crane"` 或 `"normal crane"`
  - `capacity`: 吊机起重能力，**纯数字**，单位默认为 **t**

### 分类与分配准则

1. **产线类型分类（严格）**
   - `dry dock`: 有深度维度的封闭船坞（三个维度 L×W×D）
   - `slipway`: 只有两个维度的船台（L×W）
   - AI 返回的类型如果不是这两个值，会自动触发重试

2. **吊机类型分类（严格）**
   - `gantry crane`: 龙门吊 / 门式吊机
   - `normal crane`: 普通吊机
   - AI 返回的类型如果不是这两个值，会自动触发重试

3. **产线与吊机对应准则**
   - 首先统计 `crane_parameters` 中的吊机总数
   - **数量上尽可能均分**到各条产线
   - 如果无法均分：
     - 按产线尺寸排序（有 dimensions 则比较 L×W×D 乘积，无则比较 capacity）
     - 按吊机起重能力排序（capacity 越大越强）
     - **尺寸越大的产线分配越多吊机，起重能力越强的吊机分配给越大的产线**

## 运行方式

### 前置条件
确保已设置环境变量：
```bash
set DASHSCOPE_API_KEY=your_api_key
```

### 运行脚本

```bash
# 处理所有船厂（自动跳过已经是新格式的数据）
python regenerate_production_lines.py

# 只处理前 5 条（测试用）
python regenerate_production_lines.py --limit 5

# 只处理指定船厂 ID
python regenerate_production_lines.py --shipyard-id 123

# 强制重新处理所有船厂（包括已经是新格式的）
python regenerate_production_lines.py --force
```

### 输出说明

脚本运行时会打印每个船厂的处理结果：
- `[跳过]`: 数据已经是新格式或没有产线数据
- `[验证失败]`: AI 返回的分类不合法，自动触发重试
- `[保存]`: 新数据已通过验证并写入数据库

## 文件结构

```
adjust/
├── regenerate_production_lines.py   # 主脚本
└── README.md                         # 本说明文档
```

## 注意事项

1. **备份数据**：建议在运行前备份数据库，虽然本工具只修改 `production_lines` 字段
2. **API 调用**：每次处理一个船厂会调用 1 次 AI API，大量数据请分批次处理
3. **重试机制**：如果 AI 返回的分类不合法，脚本会自动重试最多 3 次
4. **crane_parameters 不受影响**：本工具只更新 `production_lines`，`crane_parameters` 原样保留
