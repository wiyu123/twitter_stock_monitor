#!/usr/bin/env python3
"""
白毛股神serenity 推文股票监控机器人
实时抓取 serenity 最新推文中提到的股票标的，邮件通知。
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import yaml

from scraper import TwitterScraper
from extractor import extract_stocks
from tracker import StockTracker
from mailer import Mailer, load_recipients

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")


def load_config() -> dict:
    """加载 YAML 配置"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        logger.info("配置加载成功")
        return cfg
    except FileNotFoundError:
        logger.error(f"配置文件不存在: {CONFIG_FILE}")
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.error(f"配置文件解析失败: {e}")
        sys.exit(1)


class Monitor:
    """股票监控主控制器"""

    def __init__(self, config: dict):
        self.config = config

        # 初始化各模块
        tw_cfg = config.get("twitter", {})
        self.scraper = TwitterScraper(
            username=tw_cfg.get("username", ""),
            password=tw_cfg.get("password", ""),
            target_user=tw_cfg.get("target_user", "serenity"),
            proxy=tw_cfg.get("proxy"),
        )

        mon_cfg = config.get("monitor", {})
        self.interval = mon_cfg.get("interval_seconds", 90)
        self.max_tweets = mon_cfg.get("max_tweets_per_check", 10)
        self.initial_backfill = mon_cfg.get("initial_backfill", 5)

        self.tracker = StockTracker()

        smtp_cfg = config.get("smtp", {})
        self.mailer = Mailer(smtp_cfg)

        # 收件人
        email_file = config.get("email_list_file", "emails.csv")
        self.recipients = load_recipients(
            os.path.join(BASE_DIR, email_file)
        )

        self._running = True
        self._first_run = True

    async def run_once(self):
        """执行一次检查"""
        try:
            # 每次至少抓取 max_tweets 条，首次不超过 initial_backfill
            count = self.initial_backfill if self._first_run else self.max_tweets
            if count < 5:
                count = self.max_tweets
            logger.info(f"正在检查 @{self.scraper.target_user} 最新 {count} 条推文...")

            tweets = await self.scraper.get_recent_tweets(count=count)
            if not tweets:
                logger.info("未获取到新推文 (可能被限流或用户无推文)")
                return

            # 只保留最近 1 小时内发布的推文
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            recent_tweets = []
            for tweet in tweets:
                if tweet["created_at"] and tweet["created_at"] >= one_hour_ago:
                    recent_tweets.append(tweet)
            skipped = len(tweets) - len(recent_tweets)
            if skipped > 0:
                logger.info(f"跳过 {skipped} 条超过1小时的旧推文，保留 {len(recent_tweets)} 条")
            tweets = recent_tweets

            if not tweets:
                logger.info("最近1小时内无新推文")
                return

            new_send_count = 0

            for tweet in tweets:
                tweet_id = tweet["id"]
                tweet_text = tweet["text"]
                tweet_time = tweet["created_at"]
                tweet_url = tweet["url"]

                # 提取股票代码
                stocks = extract_stocks(tweet_text)
                if not stocks:
                    continue

                # 过滤已通知过的
                new_stocks = self.tracker.filter_new_stocks(stocks)
                if not new_stocks:
                    continue

                logger.info(
                    f"✨ 发现新标的! 推文 {tweet_id}: {[(c, m) for c, m in new_stocks]}"
                )

                # 发送邮件
                success = self.mailer.send_stock_alert(
                    to_addrs=self.recipients,
                    tweet_text=tweet_text,
                    tweet_url=tweet_url,
                    tweet_time=tweet_time,
                    new_stocks=new_stocks,
                    images=tweet.get("images", []),
                )

                if success:
                    # 标记为已通知
                    for code, market in new_stocks:
                        self.tracker.mark_stock_seen(code, tweet_id, tweet_time)
                    new_send_count += 1

            if new_send_count > 0:
                logger.info(f"本轮推送了 {new_send_count} 条推文")
            else:
                logger.info("本轮无新标的需要通知")

        except Exception as e:
            logger.error(f"检查异常: {e}", exc_info=True)

        finally:
            self._first_run = False

    async def run(self):
        """主循环"""
        logger.info("=" * 50)
        logger.info(f"🐂 白毛股神 serenity 推文股票监控启动")
        logger.info(f"   目标用户: @{self.scraper.target_user}")
        logger.info(f"   检查间隔: {self.interval} 秒")
        logger.info(f"   收件人: {len(self.recipients)} 个")
        logger.info(f"   已记录标的: {len(self.tracker.get_all_seen())} 个")
        logger.info("=" * 50)

        # 验证收件人
        if not self.recipients:
            logger.warning("⚠️  收件人列表为空！请检查 emails.csv")

        while self._running:
            await self.run_once()

            if not self._running:
                break

            # 等待下次检查
            logger.info(f"等待 {self.interval} 秒后进行下次检查... (Ctrl+C 退出)")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    def stop(self):
        """停止监控"""
        logger.info("收到退出信号，正在停止...")
        self._running = False


def main():
    config = load_config()
    monitor = Monitor(config)

    # 处理退出信号
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        monitor.stop()

    try:
        loop.add_signal_handler(signal.SIGINT, handle_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler，用 signal 模块处理
        signal.signal(signal.SIGINT, lambda s, f: monitor.stop())
        signal.signal(signal.SIGTERM, lambda s, f: monitor.stop())

    try:
        loop.run_until_complete(monitor.run())
    except KeyboardInterrupt:
        logger.info("用户中断 (Ctrl+C)")
    finally:
        logger.info("监控已停止")

        # 取消所有 pending 任务
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass

        # 关闭 scraper
        try:
            loop.run_until_complete(monitor.scraper.close())
        except Exception:
            pass

        loop.close()
        logger.info("再见 👋")


if __name__ == "__main__":
    main()
