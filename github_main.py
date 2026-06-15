#!/usr/bin/env python3
"""
GitHub Actions 版入口 — 长运行模式。
每 170 秒检查一次，最多 350 分钟后正常退出。
自触发由 workflow 在 cache save 后完成，无竞态。
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

SLEEP_SECONDS = 180
MAX_RUNTIME_SECONDS = 350 * 60  # 350 分钟，远小于 6h 上限


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
        logger.error("未设置 SMTP_USER 或 SMTP_PASS！")
        sys.exit(1)

    recipients = _load_recipients_live()
    if not recipients:
        logger.error("收件人列表为空")
        sys.exit(1)

    scraper = TwitterScraper(
        target_user=os.getenv("TARGET_USER", "aleabitoreddit"),
        proxy=os.getenv("TWITTER_PROXY", "").strip() or None,
        auth_token=os.getenv("X_AUTH_TOKEN", ""),
    )
    tracker = StockTracker()
    mailer = Mailer(smtp_cfg)

    logger.info(f"🚀 监控启动 | 间隔={SLEEP_SECONDS}s | 最长={MAX_RUNTIME_SECONDS // 60}m")

    start_time = datetime.now()

    try:
        iteration = 0
        while True:
            iteration += 1
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"── 第 {iteration} 轮 (已运行 {int(elapsed // 60)}m) ──")

            recipients = _load_recipients_live()
            if not recipients:
                logger.warning("收件人为空，跳过")
                await asyncio.sleep(SLEEP_SECONDS)
                continue

            try:
                tweets = await scraper.get_recent_tweets(count=10)
                if tweets:
                    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
                    recent = [t for t in tweets if t["created_at"] and t["created_at"] >= one_hour_ago]
                    new_tweets = [t for t in recent if not tracker.is_tweet_processed(t["id"])]
                    if new_tweets:
                        logger.info(f"发现 {len(new_tweets)} 篇新推文")
                        for tweet in new_tweets:
                            stocks = extract_stocks(tweet["text"])
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
                logger.error(f"检查异常: {e}", exc_info=True)

            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info(f"已达 {MAX_RUNTIME_SECONDS // 60}m 上限，正常退出（自触发由 workflow 接管）")
                break

            await asyncio.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
