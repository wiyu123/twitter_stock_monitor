"""
双重去重：内存 set + SQLite 数据库，两者任一命中即跳过。
"""

import json, logging, os, sqlite3

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_FILE = os.path.join(BASE_DIR, "seen_tweets.json")
DB_FILE = os.path.join(BASE_DIR, "tweets.db")


class StockTracker:

    def __init__(self):
        # 内存层
        self._mem: set = set()

        # SQLite 层
        self._db = sqlite3.connect(DB_FILE)
        self._db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
        self._db.commit()

        # 从 JSON 加载历史记录并入 SQLite（迁移）
        if os.path.exists(JSON_FILE):
            try:
                with open(JSON_FILE) as f:
                    old = json.load(f)
                if isinstance(old, list):
                    for tid in old:
                        try:
                            self._db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (tid,))
                        except Exception: pass
                    self._db.commit()
                # 不再用 JSON，重命名为备份
                try: os.rename(JSON_FILE, JSON_FILE + ".bak")
                except Exception: pass
            except Exception: pass

        # 把 SQLite 里所有已处理的 ID 加载到内存
        try:
            rows = self._db.execute("SELECT id FROM seen").fetchall()
            self._mem = {r[0] for r in rows}
        except Exception:
            self._mem = set()

        logger.info(f"双重去重已加载 {len(self._mem)} 条记录")

    def is_processed(self, tid: str) -> bool:
        # 内存先查（快）
        if tid in self._mem:
            return True
        # 再查数据库（兜底）
        try:
            row = self._db.execute("SELECT 1 FROM seen WHERE id=?", (tid,)).fetchone()
            if row:
                self._mem.add(tid)
                return True
        except Exception:
            pass
        return False

    def mark(self, tid: str):
        # 同时写入内存 + 数据库
        self._mem.add(tid)
        try:
            self._db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (tid,))
            self._db.commit()
        except Exception:
            pass

    def close(self):
        try:
            self._db.close()
        except Exception:
            pass
