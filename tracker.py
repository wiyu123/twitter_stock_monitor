"""
纯内存 + JSON 文件去重。不依赖 GitHub Cache，不提交 git。
"""

import json, os, logging

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "seen_tweets.json")

class StockTracker:

    def __init__(self):
        self._seen: set = set()
        self._load()

    def _load(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    self._seen = set(json.load(f))
            logger.info(f"已加载 {len(self._seen)} 条去重记录")
        except Exception:
            self._seen = set()

    def _save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(sorted(self._seen), f)
        except Exception:
            pass

    def is_processed(self, tid: str) -> bool:
        return tid in self._seen

    def mark(self, tid: str):
        self._seen.add(tid)
        self._save()

    def close(self):
        self._save()
