"""
单次检查即退出。由 cron 每 30 分钟调度，无自触发。
"""

import asyncio, logging, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import TwitterScraper
from extractor import extract_stocks
from tracker import StockTracker
from mailer import Mailer, load_recipients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")
BASE = os.path.dirname(os.path.abspath(__file__))


def _load_recipients():
    repo = os.getenv("GITHUB_REPOSITORY", "wiyu123/twitter_stock_monitor")
    url = f"https://raw.githubusercontent.com/{repo}/{os.getenv('GITHUB_REF_NAME','main')}/emails.csv"
    try:
        import urllib.request, tempfile
        req = urllib.request.Request(url, headers={"User-Agent": "monitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as t:
                    t.write(r.read().decode("utf-8-sig")); t.flush()
                    result = load_recipients(t.name)
                try: os.unlink(t.name)
                except OSError: pass
                if result: return result
    except Exception: pass
    return load_recipients(os.path.join(BASE, "emails.csv"))


async def main():
    cfg = {
        "host": os.getenv("SMTP_HOST","smtp.qq.com"), "port": int(os.getenv("SMTP_PORT","465")),
        "use_ssl": os.getenv("SMTP_SSL","true").lower()=="true",
        "username": os.getenv("SMTP_USER",""), "password": os.getenv("SMTP_PASS",""),
        "from_name": "Serenity提醒机器人",
    }
    if not cfg["username"] or not cfg["password"]:
        logger.error("SMTP 未配置"); sys.exit(1)

    recipients = _load_recipients()
    if not recipients: logger.warning("无收件人"); sys.exit(0)

    scraper = TwitterScraper(target_user=os.getenv("TARGET_USER","aleabitoreddit"), auth_token=os.getenv("X_AUTH_TOKEN",""))
    tracker = StockTracker()
    mailer = Mailer(cfg)

    try:
        tweets = await scraper.get_recent_tweets(count=10)
        if not tweets: return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        recent = [t for t in tweets if t["created_at"] and t["created_at"] >= cutoff]

        for t in recent:
            if tracker.is_processed(t["id"]):
                logger.debug(f"跳过已推送: {t['id']}")
                continue
            stocks = extract_stocks(t["text"])
            logger.info(f"推送: {t['id']} {[(c,m) for c,m in stocks] if stocks else '无标的'}")

            mailer.send_tweet_alert(
                to_addrs=recipients, tweet_text=t["text"], tweet_url=t["url"],
                tweet_time=t["created_at"], stocks=stocks or [],
                images=t.get("images", []),
            )
            tracker.mark(t["id"])
    except Exception as e:
        logger.error(f"异常: {e}", exc_info=True)
    finally:
        tracker.close()
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
