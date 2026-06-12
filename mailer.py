"""
邮件发送模块
通过 SMTP 发送 HTML 格式的股票提醒邮件，附带中文翻译。
收件人由 emails.csv 管理 (email, expire_date 两列)。
"""

import csv
import os
import smtplib
import ssl
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, date
from threading import Lock
from typing import List, Tuple

logger = logging.getLogger(__name__)

MIN_EMAIL_INTERVAL = 3.0
_last_send_time = 0.0


def translate_text(text: str, target: str = "zh-CN") -> str:
    """用 deep-translator (GoogleTranslate) 翻译文本为中文。

    如果翻译失败，返回空字符串（邮件中不展示翻译区域）。
    """
    if not text or len(text.strip()) < 10:
        return ""
    try:
        from deep_translator import GoogleTranslator
        # GoogleTranslator 无需 API key，但有频率限制
        result = GoogleTranslator(source="auto", target=target).translate(text)
        if result and result != text:
            return result.strip()
    except Exception as e:
        logger.debug(f"翻译失败: {e}")
    return ""


class Mailer:
    """邮件发送器"""

    def __init__(self, config: dict):
        self.host: str = config.get("host", "smtp.qq.com")
        self.port: int = config.get("port", 465)
        self.use_ssl: bool = config.get("use_ssl", True)
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.from_name: str = config.get("from_name", "股票监控机器人")
        self._send_lock = Lock()

    # ── 公开接口 ──

    def send_stock_alert(
        self,
        to_addrs: List[str],
        tweet_text: str,
        tweet_url: str,
        tweet_time: datetime,
        new_stocks: list,
    ) -> bool:
        """并行发送邮件给多位收件人，线程池大小 1~50 自适应。

        每个收件人独立连接、独立发送，互不阻塞。
        线程池大小 = min(max(1, 收件人数 // 5), 50)。
        """
        if not to_addrs:
            logger.warning("收件人列表为空，跳过发送")
            return False
        if not new_stocks:
            return False

        stock_codes = ", ".join([code for code, _ in new_stocks])
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        subject = f"[股神监控] 新标的提醒 - {stock_codes} - {now_str}"

        # 翻译 & HTML 构建只做一次（线程安全）
        translation = translate_text(tweet_text)
        html = self._build_html(tweet_text, tweet_url, tweet_time, new_stocks, translation)

        n = len(to_addrs)
        # ── 线程池大小：每 5 人 1 线程，最少 1，最多 50 ──
        pool_size = max(1, min(n // 5 + (1 if n % 5 else 0), 50))
        logger.info(f"开始并行发送 {n} 封邮件 (线程池: {pool_size})")

        success, fail = 0, 0
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = {
                executor.submit(
                    self._send_one, addr, subject, html
                ): addr
                for addr in to_addrs
            }
            for fut in as_completed(futures):
                addr = futures[fut]
                try:
                    ok = fut.result()
                    if ok:
                        success += 1
                    else:
                        fail += 1
                except Exception as e:
                    logger.error(f"线程异常 ({addr}): {e}")
                    fail += 1

        logger.info(f"邮件发送完毕: 成功={success} 失败={fail}")
        return success > 0

    # ── 内部实现 ──

    def _send_one(self, to_addr: str, subject: str, html: str) -> bool:
        """单线程：构建一封邮件并发送给单个收件人（带重试）。"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.username
        msg["To"] = to_addr
        msg.attach(MIMEText(html, "html", "utf-8"))

        for attempt in range(3):
            try:
                if self.use_ssl:
                    with smtplib.SMTP_SSL(
                        self.host, self.port, timeout=30,
                        context=ssl.create_default_context(),
                    ) as server:
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                        server.ehlo()
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())

                logger.info(f"已发送 → {to_addr}")
                return True

            except smtplib.SMTPAuthenticationError:
                logger.error(f"SMTP 认证失败: {to_addr}")
                return False
            except Exception as e:
                err_msg = str(e)
                wait = 30 + attempt * 30 if ("Too many" in err_msg or "limit" in err_msg.lower()) else 5
                logger.warning(f"发送失败 {to_addr} ({err_msg[:50]})，{wait}s 后重试 ({attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(wait)

        logger.error(f"发送失败 (已重试3次): {to_addr}")
        return False

    def _build_html(
        self,
        tweet_text: str,
        tweet_url: str,
        tweet_time: datetime,
        new_stocks: list,
        translation: str = "",
    ) -> str:
        time_str = tweet_time.strftime("%Y-%m-%d %H:%M:%S") if tweet_time else "未知"

        stock_items = ""
        market_emoji = {
            "A股(上海主板)": "🏛️", "A股(深圳主板)": "🏛️",
            "A股(创业板)": "🔬", "A股(科创板)": "🚀",
            "港股": "🇭🇰", "美股": "🇺🇸",
        }
        for code, market in new_stocks:
            emoji = market_emoji.get(market, "📌")
            stock_items += (
                f'<li style="margin:6px 0;">'
                f'<span style="font-size:16px;">{emoji} '
                f'<strong style="color:#d4380d;">{code}</strong></span> '
                f'<span style="color:#888;">({market})</span></li>\n'
            )

        safe_text = tweet_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # 翻译区块（只有翻译成功才显示）
        translation_block = ""
        if translation:
            safe_trans = translation.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            translation_block = f"""
          <div style="padding:14px 18px;margin-top:12px;background:#f6ffed;border-left:4px solid #52c41a;border-radius:4px;line-height:1.7;color:#333;white-space:pre-wrap;word-break:break-word;">
            <div style="font-size:11px;color:#52c41a;margin-bottom:4px;">🌐 中文翻译</div>
            {safe_trans}
          </div>"""

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">

  <div style="background:linear-gradient(135deg,#1677ff,#0958d9);padding:20px 24px;color:#fff;">
    <h2 style="margin:0;font-size:20px;">🐂 白毛股神 serenity 发推提到新标的</h2>
    <p style="margin:6px 0 0;opacity:0.85;font-size:13px;">{time_str}</p>
  </div>

  <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
    <h3 style="margin:0 0 12px;color:#333;font-size:16px;">📊 发现的股票标的</h3>
    <ul style="padding-left:20px;margin:0;">
{stock_items}
    </ul>
  </div>

  <div style="padding:20px 24px;">
    <h3 style="margin:0 0 10px;color:#333;font-size:16px;">📝 推文原文</h3>
    <blockquote style="margin:0;padding:14px 18px;background:#fafafa;border-left:4px solid #1677ff;border-radius:4px;line-height:1.7;color:#555;white-space:pre-wrap;word-break:break-word;">
{safe_text}
    </blockquote>
    {translation_block}
    <p style="margin-top:14px;">
      <a href="{tweet_url}" style="color:#1677ff;text-decoration:none;font-size:13px;" target="_blank">
        🔗 查看原文 → {tweet_url}
      </a>
    </p>
  </div>

  <div style="padding:14px 24px;background:#fafafa;border-top:1px solid #f0f0f0;text-align:center;font-size:12px;color:#999;">
    股票监控机器人 | 生成于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
  </div>

</div>
</body>
</html>"""
        return html


def load_recipients(filepath: str) -> List[str]:
    """从 CSV 文件加载收件人邮箱（去重 + 过滤过期）

    CSV 格式:
        email,expire_date
        356487812@qq.com,2027-12-31

    规则:
      - 自动跳过空行和表头(第一列不是合法邮箱的行)
      - 失效日期为空或超过今天的 → 加入列表
      - 已过期 → 跳过
      - 最终去重
    """
    recipients = []
    if not os.path.exists(filepath):
        logger.error(f"收件人文件不存在: {filepath}")
        return recipients

    today = date.today()
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                email = row[0].strip()
                if "@" not in email:
                    continue  # 跳过表头/注释行

                # 失效日期 (可选)
                expire_str = (row[1].strip() if len(row) > 1 else "").strip()
                if expire_str:
                    try:
                        expire_date = datetime.strptime(expire_str, "%Y-%m-%d").date()
                        if expire_date < today:
                            logger.debug(f"收件人已过期: {email} ({expire_str})")
                            continue
                    except ValueError:
                        logger.warning(f"无法解析失效日期 '{expire_str}'，跳过: {email}")
                        continue

                recipients.append(email)

    except Exception as e:
        logger.error(f"读取收件人文件失败: {e}")
        return []

    # 去重（保留首次出现的顺序）
    seen = set()
    unique = []
    for r in recipients:
        r_lower = r.lower()
        if r_lower not in seen:
            seen.add(r_lower)
            unique.append(r)

    logger.info(f"加载收件人 {len(unique)} 个: {unique}")
    return unique
