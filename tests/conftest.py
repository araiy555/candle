import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os

os.environ["CANDLE_NO_HISTORY"] = "1"      # テスト中は、過去データを外部から取りに行かない
