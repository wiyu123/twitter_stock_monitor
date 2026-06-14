#!/usr/bin/env python3
"""
GitHub Actions 版入口 — 单次调度启动，内部循环运行。
每 5 分钟检查一次，110 分钟后自动退出等待下次 cron 重启。
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

SLEEP_SECONDS = 300                # 5 分钟
MAX_RUNTIME_SECONDS = 110 * 60     # 110 分钟，cron 2h 前退出防重叠


def _load_recipients_live() -> List[str]:
    """从 GitHub raw URL 获取最新 emails.csv，失败时 fallback 到本地。"""
    repo = os.getenv("GITHUB_REPOSITORY", "wiyu123/twitter_stock_monitor")
    ref = os.getenv("GITHUB_REF_NAME", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/emails.csv"

    try:
        import urllib.request
        import tempfile
        req = urllib.request.Request(url, headers={"User-Agent": "stock-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = resp.read().decode("utf-8-sig")
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".csv", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name
                result = load_recipients(tmp_path)
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                if result:
                    logger.info(f"从 GitHub 加载最新收件人: {len(result)} 人")
                    return result
    except Exception as e:
        logger.debug(f"GitHub raw 拉取失败: {e}，回退到本地文件")

    return load_recipients(os.path.join(BASE_DIR, "emails.csv"))


def get_smtp_config() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "use_ssl": os.getenv("SMTP_SSL", "true").lower() == "true",
        "username": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
        "from_name": "股票监控机器人",
    }


async def run_check(scraper, tracker, mailer, recipients):
    """单次检查 → 抓推文 → 过滤 → 发邮件"""
    try:
        tweets = await scraper.get_recent_tweets(count=10)
        if not tweets:
            logger.info("未获取到推文")
            return

        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in tweets if t["created_at"] and t["created_at"] >= one_hour_ago]
        if len(recent) < len(tweets):
            logger.info(f"跳过 {len(tweets) - len(recent)} 条旧推文，保留 {len(recent)} 条")

        if not recent:
            logger.info("最近 1 小时内无新推文")
            return

        new_tweets = []
        for t in recent:
            if tracker.is_tweet_processed(t["id"]):
                continue
            new_tweets.append(t)

        if not new_tweets:
            return

        logger.info(f"发现 {len(new_tweets)} 篇未处理的推文")

        for tweet in new_tweets:
            stocks = extract_stocks(tweet["text"])
            label = f"{[(c, m) for c, m in stocks]}" if stocks else "无股票标的"
            logger.info(f"新推文! {tweet['id']}: {label}")

            success = mailer.send_tweet_alert(
                to_addrs=recipients,
                tweet_text=tweet["text"],
                tweet_url=tweet["url"],
                tweet_time=tweet["created_at"],
                stocks=stocks or [],
                images=tweet.get("images", []),
            )

            tracker.mark_tweet_done(tweet["id"], tweet["created_at"])
            if success:
                logger.info("邮件已发送")

    except Exception as e:
        logger.error(f"检查异常: {e}", exc_info=True)


async def main():
    smtp_cfg = get_smtp_config()
    if not smtp_cfg.get("username") or not smtp_cfg.get("password"):
        logger.error("未设置 SMTP_USER 或 SMTP_PASS 环境变量！")
        sys.exit(1)

    recipients = _load_recipients_live()
    if not recipients:
        logger.error("收件人列表为空")
        sys.exit(1)

    target_user = os.getenv("TARGET_USER", "aleabitoreddit")

    scraper = TwitterScraper(
        target_user=target_user,
        proxy=os.getenv("TWITTER_PROXY", "").strip() or None,
        auth_token=os.getenv("X_AUTH_TOKEN", ""),
    )
    tracker = StockTracker()
    mailer = Mailer(smtp_cfg)

    logger.info(f"🚀 监控启动 @{target_user} | 间隔={SLEEP_SECONDS}s | 最长={MAX_RUNTIME_SECONDS // 60}m")

    start_time = datetime.now()

    try:
        iteration = 0
        while True:
            iteration += 1
            elapsed = (datetime.now() - start_time).total_seconds()

            logger.info(f"── 第 {iteration} 轮检查 (已运行 {int(elapsed // 60)}m) ──")
            recipients = _load_recipients_live()
            if not recipients:
                logger.warning("收件人列表为空，跳过本轮")
                await asyncio.sleep(SLEEP_SECONDS)
                continue
            await run_check(scraper, tracker, mailer, recipients)

            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info(f"已达 {MAX_RUNTIME_SECONDS // 60}m 上限，正常退出")
                break

            logger.info(f"等待 {SLEEP_SECONDS // 60} 分钟...")
            await asyncio.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
