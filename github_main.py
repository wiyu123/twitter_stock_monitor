#!/usr/bin/env python3
"""
GitHub Actions 版 — 单次检查即退出，由 cron 高频调度。
每次运行 ≤30 秒，挂了不影响，下次 cron 自动补上。
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import TwitterScraper
from extractor import extract_stocks
from tracker import StockTracker
from mailer import Mailer, load_recipients

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("github-monitor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_recipients_live() -> List[str]:
    repo = os.getenv("GITHUB_REPOSITORY", "wiyu123/twitter_stock_monitor")
    ref = os.getenv("GITHUB_REF_NAME", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/emails.csv"
    try:
        import urllib.request, tempfile
        req = urllib.request.Request(url, headers={"User-Agent": "stock-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = resp.read().decode("utf-8-sig")
                with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as tmp:
                    tmp.write(data); tmp_path = tmp.name
                result = load_recipients(tmp_path)
                try: os.unlink(tmp_path)
                except OSError: pass
                if result:
                    return result
    except Exception:
        pass
    return load_recipients(os.path.join(BASE_DIR, "emails.csv"))


def get_smtp_config() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "use_ssl": os.getenv("SMTP_SSL", "true").lower() == "true",
        "username": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
        "from_name": "Serenity提醒机器人",
    }


async def main():
    smtp_cfg = get_smtp_config()
    if not smtp_cfg.get("username") or not smtp_cfg.get("password"):
        logger.error("未设置 SMTP 环境变量！")
        sys.exit(1)

    recipients = _load_recipients_live()
    if not recipients:
        logger.warning("收件人列表为空，跳过")
        sys.exit(0)

    scraper = TwitterScraper(
        target_user=os.getenv("TARGET_USER", "aleabitoreddit"),
        proxy=None,
        auth_token=os.getenv("X_AUTH_TOKEN", ""),
    )
    tracker = StockTracker()
    mailer = Mailer(smtp_cfg)

    try:
        tweets = await scraper.get_recent_tweets(count=10)
        if not tweets:
            logger.info("无推文")
            return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in tweets if t["created_at"] and t["created_at"] >= cutoff]
        new = [t for t in recent if not tracker.is_tweet_processed(t["id"])]

        if not new:
            return

        logger.info(f"发现 {len(new)} 篇新推文")
        for tweet in new:
            stocks = extract_stocks(tweet["text"])
            label = f"{[(c,m) for c,m in stocks]}" if stocks else "无标的"
            logger.info(f"推送: {tweet['id']} {label}")
            mailer.send_tweet_alert(
                to_addrs=recipients,
                tweet_text=tweet["text"],
                tweet_url=tweet["url"],
                tweet_time=tweet["created_at"],
                stocks=stocks or [],
                images=tweet.get("images", []),
            )
            tracker.mark_tweet_done(tweet["id"], tweet["created_at"])

    except Exception as e:
        logger.error(f"异常: {e}", exc_info=True)
    finally:
        tracker.close()
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
