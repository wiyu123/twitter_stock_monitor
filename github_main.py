#!/usr/bin/env python3
"""
内部循环 110 分钟，每 2 分钟检查一次，退出后由 workflow 自触发续命。
"""

import asyncio, hashlib, logging, os, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import TwitterScraper
from extractor import extract_stocks
from tracker import StockTracker
from mailer import Mailer, load_recipients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("github-monitor")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INTERVAL = 120          # 2 分钟
MAX_RUNTIME = 110 * 60  # 110 分钟
RECENT_SEND_WINDOW = 600  # 10 分钟内同内容 hash 不重复发


def _load_recipients_live():
    repo = os.getenv("GITHUB_REPOSITORY", "wiyu123/twitter_stock_monitor")
    url = f"https://raw.githubusercontent.com/{repo}/{os.getenv('GITHUB_REF_NAME','main')}/emails.csv"
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
                if result: return result
    except Exception: pass
    return load_recipients(os.path.join(BASE_DIR, "emails.csv"))


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


async def main():
    smtp_cfg = {
        "host": os.getenv("SMTP_HOST", "smtp.qq.com"), "port": int(os.getenv("SMTP_PORT", "465")),
        "use_ssl": os.getenv("SMTP_SSL", "true").lower() == "true",
        "username": os.getenv("SMTP_USER", ""), "password": os.getenv("SMTP_PASS", ""),
        "from_name": "Serenity提醒机器人",
    }
    if not smtp_cfg["username"] or not smtp_cfg["password"]:
        logger.error("未设置 SMTP"); sys.exit(1)

    recipients = _load_recipients_live()
    if not recipients: logger.error("收件人为空"); sys.exit(1)

    scraper = TwitterScraper(target_user=os.getenv("TARGET_USER", "aleabitoreddit"), auth_token=os.getenv("X_AUTH_TOKEN", ""))
    tracker = StockTracker()
    mailer = Mailer(smtp_cfg)

    recent_hashes: dict = {}  # hash → timestamp，10 分钟内不重发
    sent_ids: set = set()     # 本进程内存已发 ID

    logger.info(f"🚀 启动 | 间隔={INTERVAL}s | 最长={MAX_RUNTIME//60}m")
    start = datetime.now()

    try:
        iteration = 0
        while True:
            iteration += 1
            elapsed = (datetime.now() - start).total_seconds()
            logger.info(f"── 第 {iteration} 轮 ({int(elapsed//60)}m) ──")

            # 清理过期 hash 记录
            now_ts = time.time()
            recent_hashes = {h: ts for h, ts in recent_hashes.items() if now_ts - ts < RECENT_SEND_WINDOW}

            recipients = _load_recipients_live()
            if not recipients:
                await asyncio.sleep(INTERVAL); continue

            try:
                tweets = await scraper.get_recent_tweets(count=10)
                if tweets:
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
                    recent = [t for t in tweets if t["created_at"] and t["created_at"] >= cutoff]
                    new = [t for t in recent
                           if not tracker.is_tweet_processed(t["id"])
                           and t["id"] not in sent_ids
                           and _hash(t["text"]) not in recent_hashes]
                    if new:
                        logger.info(f"发现 {len(new)} 篇新推文")
                        for tweet in new:
                            h = _hash(tweet["text"])
                            mailer.send_tweet_alert(
                                to_addrs=recipients, tweet_text=tweet["text"],
                                tweet_url=tweet["url"], tweet_time=tweet["created_at"],
                                stocks=extract_stocks(tweet["text"]) or [],
                                images=tweet.get("images", []),
                            )
                            tracker.mark_tweet_done(tweet["id"], tweet["created_at"])
                            sent_ids.add(tweet["id"])
                            recent_hashes[h] = now_ts
            except Exception as e:
                logger.error(f"检查异常: {e}", exc_info=True)

            if elapsed >= MAX_RUNTIME:
                logger.info(f"已达 {MAX_RUNTIME//60}m 上限，正常退出")
                break
            await asyncio.sleep(INTERVAL)

    except KeyboardInterrupt: pass
    finally:
        tracker.close()
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
