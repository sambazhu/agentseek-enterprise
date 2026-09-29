import sys
from pathlib import Path

import pytest

# sandbox_poc（M0 PoC 组件包）位于样例根目录，测试需要可导入它。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def sandbox_tool_logs():
    """Capture the real application logging route, without a stdlib bridge."""
    from loguru import logger
    records = []
    sink = logger.add(lambda message: records.append(message.record.copy()), level="INFO",
                      filter=lambda record: record["name"] == "enterprise_wecom_digital_employee.sandbox_remote",
                      format="{message}")
    try:
        yield records
    finally:
        logger.remove(sink)
