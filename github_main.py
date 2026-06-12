#!/usr/bin/env python3
"""
GitHub Actions 版入口 — 单次调度启动，内部循环运行 6 小时。
公开仓库 GitHub Actions 分钟不限量，6 小时是单次任务上限。
每 300 秒 (5 分钟) 执行一次检查，到达 6 小时后自动退出等待下次 cron 重启。
"""

import asyncio
import logging
import os
import sys
import subprocess
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

SLEEP_SECONDS = 300        # 5 分钟
MAX_RUNTIME_SECONDS = 6 * 3600  # 6 小时 (GitHub Actions 单次上限)


def _trigger_next_run():
    """触发下一次 workflow_dispatch，实现无缝衔接"""
    token = os.getenv("GH_TOKEN", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    ref = os.getenv("GITHUB_REF_NAME", "main")
    workflow_file = os.getenv("GITHUB_WORKFLOW_REF", "").split("@")[0]

    if not token or not repo:
        logger.warning("缺少 GH_TOKEN，无法自触发下一轮")
        return

    try:
        # 用 GitHub Actions 内置的 GITHUB_TOKEN 调度新的 workflow_dispatch
        # monitor.yml 的 workflow ID 通过文件名方式调用
        url = f"https://api.github.com/repos/{repo}/actions/workflows/monitor.yml/dispatches"
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-X", "POST",
                "-H", "Accept: application/vnd.github+json",
                "-H", f"Authorization: Bearer {token}",
                url,
                "-d", f'{{"ref":"{ref}"}}',
            ],
            capture_output=True, text=True, timeout=10,
        )
        if "20" in result.stdout:
            logger.info("已触发下一轮，无缝衔接")
        else:
            logger.warning(f"触发下一轮失败: HTTP {result.stdout}")
    except Exception as e:
        logger.warning(f"触发下一轮异常: {e}")


def _load_recipients_live() -> List[str]:
    """从 GitHub raw URL 获取最新 emails.csv，失败时fallback到本地文件。

    公开仓库无需认证，直接走 HTTPS。不在中国所以不走代理。
    每 5 分钟调用一次，这样你在 GitHub 上修改 emails.csv 后
    下次检查周期自动生效，不需要重启 Action。
    """
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
                # 写到临时文件再用 load_recipients 解析
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

    # Fallback: 本地 CSV
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

        # 只保留最近 1 小时内发布的
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
                logger.info(f"邮件已发送")

    except Exception as e:
        logger.error(f"检查异常: {e}", exc_info=True)


async def main():
    smtp_cfg = get_smtp_config()
    if not smtp_cfg.get("username") or not smtp_cfg.get("password"):
        logger.error("未设置 SMTP_USER 或 SMTP_PASS 环境变量！")
        sys.exit(1)

    # 初始加载
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

    logger.info(f"🚀 监控启动 @{target_user} | 间隔={SLEEP_SECONDS}s | 最长运行={MAX_RUNTIME_SECONDS//3600}h")

    start_time = datetime.now()

    try:
        iteration = 0
        while True:
            iteration += 1
            elapsed = (datetime.now() - start_time).total_seconds()

            logger.info(f"── 第 {iteration} 轮检查 (已运行 {int(elapsed // 60)}m) ──")
            # 每轮重新拉取收件人，GitHub 上改 emails.csv 后 5 分钟内生效
            recipients = _load_recipients_live()
            if not recipients:
                logger.warning("收件人列表为空，跳过本轮")
                await asyncio.sleep(SLEEP_SECONDS)
                continue
            await run_check(scraper, tracker, mailer, recipients)

            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info(f"已达 {MAX_RUNTIME_SECONDS // 3600} 小时上限，触发下一轮后退出")
                _trigger_next_run()
                break

            logger.info(f"等待 {SLEEP_SECONDS // 60} 分钟...")
            await asyncio.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
