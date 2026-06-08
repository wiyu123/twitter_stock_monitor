"""
已推送股票追踪模块
负责 seen_stocks.json 的读写，实现去重：同一股票代码只通知一次。
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_stocks.json")


class StockTracker:
    """已通知股票追踪器"""

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._seen: Dict[str, dict] = {}
        self._load()

    def _load(self):
        """从 JSON 文件加载已通知记录"""
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    self._seen = json.load(f)
                logger.info(f"加载已通知记录: {len(self._seen)} 个标的")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 seen_stocks.json 失败: {e}，使用空记录")
                self._seen = {}
        else:
            self._seen = {}

    def _save(self):
        """保存记录到 JSON 文件"""
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self._seen, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存 seen_stocks.json 失败: {e}")

    def is_new(self, code: str) -> bool:
        """检查股票代码是否为首次出现"""
        code = code.upper()
        return code not in self._seen

    def mark_seen(self, code: str, tweet_id: str, tweet_time: Optional[datetime] = None):
        """标记股票代码已通知"""
        code = code.upper()
        self._seen[code] = {
            "tweet_id": tweet_id,
            "time": tweet_time.isoformat() if tweet_time else datetime.now().isoformat(),
        }
        self._save()
        logger.info(f"已标记: {code} (推文: {tweet_id})")

    def filter_new_stocks(self, stocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """过滤出尚未通知过的股票代码

        Args:
            stocks: [(code, market), ...] 从 extractor 返回的列表

        Returns:
            list[(code, market)]: 新的股票代码 (首次出现)
        """
        new_stocks = []
        for code, market in stocks:
            if self.is_new(code):
                new_stocks.append((code, market))
            else:
                logger.debug(f"跳过已通知: {code}")
        return new_stocks

    def get_all_seen(self) -> Dict[str, dict]:
        """获取所有已通知记录"""
        return dict(self._seen)
