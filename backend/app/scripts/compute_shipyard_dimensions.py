# -*- coding: utf-8 -*-
"""
船厂六维评分计算脚本（app/scripts 入口，逻辑在 app.shipyard_dimensions 包内）。

用法：
    cd <repo>/backend
    source .venv/bin/activate

    # 小范围试跑（推荐先用 --shipyard-id 验证）
    python app/scripts/compute_shipyard_dimensions.py --shipyard-id 423

    # 全部有订单映射的船厂
    python app/scripts/compute_shipyard_dimensions.py --all

    # 重新拟合 NNLS 权重后全量计算
    python app/scripts/compute_shipyard_dimensions.py --all --fit-weights

完整参数见 python app/scripts/compute_shipyard_dimensions.py --help
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.shipyard_dimensions.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
