import sys
from pathlib import Path

# sandbox_poc（M0 PoC 组件包）位于样例根目录，测试需要可导入它。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
