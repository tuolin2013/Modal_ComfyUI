import sys
from pathlib import Path

# pipeline 里的模块按脚本目录互相 import（如 import ecom_images），测试时把 pipeline/ 放进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
