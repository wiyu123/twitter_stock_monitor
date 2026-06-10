"""
已推送跟踪模块
seen_stocks.json 记录 {股票代码 → 信息}，去重同一标的。
seen_tweets.json 记录 {推文ID → 时间}，去重已处理过的推文。
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOCKS_FILE = os.path.join(BASE_DIR, "seen_stocks.json")
TWEETS_FILE = os.path.join(BASE_DIR, "seen_tweets.json")


class StockTracker:
    """已通知追踪器（股票代码 + 推文ID 双层去重）"""

    def __init__(self):
        self._stocks_seen: Dict[str, dict] = {}   # code → {tweet_id, time}
        self._tweets_seen: Dict[str, str] = {}     # tweet_id → time
        self._load()

    # ── 文件读写 ──

    def _load(self):
        if os.path.exists(STOCKS_FILE):
            try:
                with open(STOCKS_FILE, "r", encoding="utf-8") as f:
                    self._stocks_seen = json.load(f)
                logger.info(f"加载已通知标的: {len(self._stocks_seen)} 个")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 seen_stocks.json 失败: {e}")
                self._stocks_seen = {}

        if os.path.exists(TWEETS_FILE):
            try:
                with open(TWEETS_FILE, "r", encoding="utf-8") as f:
                    self._tweets_seen = json.load(f)
                logger.info(f"加载已处理推文: {len(self._tweets_seen)} 篇")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 seen_tweets.json 失败: {e}")
                self._tweets_seen = {}

    def _save_stocks(self):
        try:
            with open(STOCKS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._stocks_seen, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存 seen_stocks.json 失败: {e}")

    def _save_tweets(self):
        try:
            with open(TWEETS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._tweets_seen, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存 seen_tweets.json 失败: {e}")

    # ── 推文级去重 ──

    def is_tweet_processed(self, tweet_id: str) -> bool:
        """检查推文 ID 是否已处理过"""
        return tweet_id in self._tweets_seen

    def mark_tweet_done(self, tweet_id: str, tweet_time: Optional[datetime] = None):
        """标记推文已处理"""
        self._tweets_seen[tweet_id] = tweet_time.isoformat() if tweet_time else datetime.now().isoformat()
        # 只保留最近 500 条，避免文件过大
        if len(self._tweets_seen) > 500:
            # 删除最旧的
            sorted_ids = sorted(self._tweets_seen.items(), key=lambda x: x[1])
            for old_id, _ in sorted_ids[: len(self._tweets_seen) - 500]:
                del self._tweets_seen[old_id]
        self._save_tweets()

    # ── 股票代码去重 ──

    def is_stock_new(self, code: str) -> bool:
        code = code.upper()
        return code not in self._stocks_seen

    def filter_new_stocks(self, stocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
        new_stocks = []
        for code, market in stocks:
            if self.is_stock_new(code):
                new_stocks.append((code, market))
            else:
                logger.debug(f"跳过已通知标的: {code}")
        return new_stocks

    def mark_stock_seen(self, code: str, tweet_id: str, tweet_time: Optional[datetime] = None):
        code = code.upper()
        self._stocks_seen[code] = {
            "tweet_id": tweet_id,
            "time": tweet_time.isoformat() if tweet_time else datetime.now().isoformat(),
        }
        self._save_stocks()
        logger.debug(f"已标记标的: {code}")
