"""
单进程 350 分钟循环，内存去重 100% 不重复。不自触发。
cron 6h 保底，退出前持久化到 JSON 文件供下个进程冷启动。
"""

import asyncio, logging, os, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import TwitterScraper
from extractor import extract_stocks
from mailer import Mailer, load_recipients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("monitor")
BASE = os.path.dirname(os.path.abspath(__file__))

INTERVAL = 120          # 2 分钟
MAX_RUNTIME = 350 * 60  # 350 分钟
STATE_FILE = os.path.join(BASE, "seen_tweets.json")


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
    if not recipients: logger.error("无收件人"); sys.exit(1)

    scraper = TwitterScraper(target_user=os.getenv("TARGET_USER","aleabitoreddit"), auth_token=os.getenv("X_AUTH_TOKEN",""))
    mailer = Mailer(cfg)

    # ── 内存去重（单进程内 100% 不重复）──
    sent_cache: list = []
    sent_set: set = set()
    if os.path.exists(STATE_FILE):
        try:
            import json
            with open(STATE_FILE) as f:
                sent_cache = json.load(f)
            if isinstance(sent_cache, list):
                sent_set = set(sent_cache)
            logger.info(f"从 {STATE_FILE} 加载 {len(sent_set)} 条历史记录")
        except Exception:
            sent_cache, sent_set = [], set()

    # ── 自动清理旧记录（保留最近 2000 条）──
    def _save():
        try:
            with open(STATE_FILE, "w") as f:
                import json
                json.dump(sent_cache[-2000:], f)
        except Exception:
            pass

    # ── 健康检查：如果 3 个周期内没有网络请求成功，主动退出 ──
    health_errors = 0
    MAX_HEALTH_ERRORS = 3

    logger.info(f"🚀 单进程启动 | 间隔={INTERVAL}s | 最长={MAX_RUNTIME//60}m | url={len(sent_set)}条记录")
    start = datetime.now()

    try:
        iteration = 0
        while True:
            iteration += 1
            elapsed = (datetime.now() - start).total_seconds()

            recipients = _load_recipients()
            if not recipients:
                await asyncio.sleep(INTERVAL); continue

            try:
                tweets = await scraper.get_recent_tweets(count=10)
                if tweets:
                    health_errors = 0  # 成功获取，重置健康计数
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
                    recent = [t for t in tweets if t["created_at"] and t["created_at"] >= cutoff]
                    for t in recent:
                        if t["id"] in sent_set:
                            continue
                        logger.info(f"[{elapsed//60:.0f}m] 推送: {t['id']}")
                        stocks = extract_stocks(t["text"])
                        mailer.send_tweet_alert(
                            to_addrs=recipients, tweet_text=t["text"], tweet_url=t["url"],
                            tweet_time=t["created_at"], stocks=stocks or [],
                            images=t.get("images", []),
                        )
                        sent_set.add(t["id"])
                        sent_cache.append(t["id"])
                else:
                    health_errors += 1
                    logger.warning(f"无推文返回 (健康计数: {health_errors}/{MAX_HEALTH_ERRORS})")
            except Exception as e:
                health_errors += 1
                logger.error(f"检查异常 (健康计数: {health_errors}/{MAX_HEALTH_ERRORS}): {e}")

            _save()

            if health_errors >= MAX_HEALTH_ERRORS:
                logger.error(f"连续 {MAX_HEALTH_ERRORS} 次失败，主动退出等待 cron 重启")
                break

            if elapsed >= MAX_RUNTIME:
                logger.info(f"已达 {MAX_RUNTIME//60}m 上限，正常退出")
                break

            await asyncio.sleep(INTERVAL)

    except KeyboardInterrupt:
        pass
    finally:
        _save()
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
