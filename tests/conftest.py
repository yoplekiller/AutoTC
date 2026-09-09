import os
import sys

# src/ 내부 모듈들이 전부 "from tc_core import ..." 같은 bare import를 쓰므로
# (python src/xxx.py로 직접 실행할 때 스크립트 디렉터리가 자동으로 sys.path에 잡히는 것과 동일하게)
# 테스트에서도 src/ 자체를 sys.path에 넣어준다.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
