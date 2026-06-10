#!/usr/bin/env python3
"""
GitHub Actions 版入口 — 单次执行，由 cron 每 5 分钟调度。
从环境变量读取密钥，不依赖 config.yaml 中的 SMTP 密码。
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# 确保当前目录在 path 中
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


def get_smtp_config() -> dict:
    """从环境变量读取 SMTP 配置"""
    return {
        "host": os.getenv("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "use_ssl": os.getenv("SMTP_SSL", "true").lower() == "true",
        "username": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
        "from_name": "股票监控机器人",
    }


def get_twitter_config() -> dict:
    """从环境变量读取 Twitter 配置"""
    return {
        "target_user": os.getenv("TARGET_USER", "aleabitoreddit"),
        "proxy": os.getenv("TWITTER_PROXY", "").strip() or None,
    }


async def main():
    # 加载 SMTP 配置
    smtp_cfg = get_smtp_config()
    if not smtp_cfg.get("username") or not smtp_cfg.get("password"):
        logger.error("未设置 SMTP_USER 或 SMTP_PASS 环境变量！")
        sys.exit(1)

    # 加载 Twitter 配置
    tw_cfg = get_twitter_config()

    # 加载收件人
    recipients = load_recipients(os.path.join(BASE_DIR, "emails.txt"))
    if not recipients:
        logger.error("收件人列表为空")
        sys.exit(1)

    # 初始化模块
    scraper = TwitterScraper(
        target_user=tw_cfg["target_user"],
        proxy=tw_cfg["proxy"],  # GitHub runner 在美国，不需要代理
        auth_token=os.getenv("X_AUTH_TOKEN", ""),
    )
    tracker = StockTracker()
    mailer = Mailer(smtp_cfg)

    logger.info(f"开始检查 @{tw_cfg['target_user']} ...")

    try:
        # 抓取最新推文
        tweets = await scraper.get_recent_tweets(count=10)
        if not tweets:
            logger.info("未获取到推文")
            return

        # 只保留最近 1 小时内的
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in tweets if t["created_at"] and t["created_at"] >= one_hour_ago]
        if len(recent) < len(tweets):
            logger.info(f"跳过 {len(tweets) - len(recent)} 条旧推文，保留 {len(recent)} 条")

        if not recent:
            logger.info("最近 1 小时内无新推文")
            return

        # 过滤已处理的推文
        new_tweets = []
        for t in recent:
            if tracker.is_tweet_processed(t["id"]):
                logger.debug(f"跳过已处理推文: {t['id']}")
                continue
            new_tweets.append(t)
        if len(new_tweets) < len(recent):
            logger.info(f"跳过 {len(recent) - len(new_tweets)} 篇已推送推文，保留 {len(new_tweets)} 篇")

        if not new_tweets:
            logger.info("无新推文")
            return

        # 处理每条推文
        sent_count = 0
        for tweet in new_tweets:
            stocks = extract_stocks(tweet["text"])
            if not stocks:
                tracker.mark_tweet_done(tweet["id"], tweet["created_at"])
                continue

            logger.info(f"新标的! 推文 {tweet['id']}: {[(c, m) for c, m in stocks]}")

            success = mailer.send_stock_alert(
                to_addrs=recipients,
                tweet_text=tweet["text"],
                tweet_url=tweet["url"],
                tweet_time=tweet["created_at"],
                new_stocks=stocks,
            )

            if success:
                sent_count += 1

            # 推文已处理，不再重复推送
            tracker.mark_tweet_done(tweet["id"], tweet["created_at"])

        logger.info(f"本轮推送 {sent_count} 篇推文")

    except Exception as e:
        logger.error(f"运行异常: {e}", exc_info=True)
        sys.exit(1)

    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
