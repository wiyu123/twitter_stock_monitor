"""
已推送跟踪模块 — SQLite 数据库，推文 ID 主键去重。
"""

import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "tweets.db")


class StockTracker:
    """已通知追踪器 — SQLite 持久化，推文 ID 主键，绝对不重复。"""

    def __init__(self):
        self._conn = sqlite3.connect(DB_FILE)
        # ── DELETE 模式：写入直接刷到主文件，不依赖 WAL ──
        # GitHub Cache 只缓存主文件，WAL 数据在进程间会丢失
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS tweets ("
            "  tweet_id TEXT PRIMARY KEY,"
            "  processed_at TEXT NOT NULL"
            ")"
        )
        self._conn.commit()
        cnt = self._conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        logger.info(f"SQLite 已加载 {cnt} 条已处理推文")

    # ── 推文级去重 (核心) ──

    def is_tweet_processed(self, tweet_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tweets WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        return row is not None

    def mark_tweet_done(self, tweet_id: str, tweet_time: Optional[datetime] = None):
        ts = tweet_time.isoformat() if tweet_time else datetime.now().isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO tweets (tweet_id, processed_at) VALUES (?, ?)",
            (tweet_id, ts),
        )
        self._conn.commit()
        # 保留最近 1000 条
        cnt = self._conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        if cnt > 1000:
            self._conn.execute(
                "DELETE FROM tweets WHERE tweet_id IN ("
                "  SELECT tweet_id FROM tweets ORDER BY processed_at ASC "
                "  LIMIT ?"
                ")", (cnt - 1000,)
            )
            self._conn.commit()

    def close(self):
        self._conn.close()
