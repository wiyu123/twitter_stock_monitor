"""
邮件发送模块
通过 SMTP 发送 HTML 格式的股票提醒邮件。
"""

import smtplib
import ssl
import time
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)

# QQ 邮箱频率限制: 每分钟最多约 5-10 封，每封之间至少间隔
MIN_EMAIL_INTERVAL = 3.0  # 秒
_last_send_time = 0.0


class Mailer:
    """邮件发送器"""

    def __init__(self, config: dict):
        """
        Args:
            config: SMTP 配置字典，包含 host, port, use_ssl, username, password, from_name
        """
        self.host: str = config.get("host", "smtp.qq.com")
        self.port: int = config.get("port", 465)
        self.use_ssl: bool = config.get("use_ssl", True)
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.from_name: str = config.get("from_name", "股票监控机器人")

    def send_stock_alert(
        self,
        to_addrs: List[str],
        tweet_text: str,
        tweet_url: str,
        tweet_time: datetime,
        new_stocks: list,
    ) -> bool:
        """发送新标的提醒邮件

        Args:
            to_addrs: 收件人邮箱列表
            tweet_text: 推文原文
            tweet_url: 推文链接
            tweet_time: 推文时间
            new_stocks: [(code, market), ...] 新股票列表

        Returns:
            bool: 发送成功返回 True
        """
        if not to_addrs:
            logger.warning("收件人列表为空，跳过发送")
            return False

        if not new_stocks:
            return False

        stock_codes = ", ".join([code for code, _ in new_stocks])
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        subject = f"[股神监控] 新标的提醒 - {stock_codes} - {now_str}"

        # 构建 HTML 正文
        html = self._build_html(tweet_text, tweet_url, tweet_time, new_stocks)

        # 频率控制：两次发送之间至少间隔 MIN_EMAIL_INTERVAL 秒
        global _last_send_time
        elapsed = time.time() - _last_send_time
        if elapsed < MIN_EMAIL_INTERVAL:
            wait = MIN_EMAIL_INTERVAL - elapsed
            logger.debug(f"发送间隔控制，等待 {wait:.1f}s ...")
            time.sleep(wait)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        # QQ 邮箱要求纯 ASCII From，不用中文别名
        msg["From"] = self.username
        msg["To"] = ", ".join(to_addrs)
        msg.attach(MIMEText(html, "html", "utf-8"))

        # 重试最多 3 次
        last_error = None
        for attempt in range(3):
            try:
                if self.use_ssl:
                    with smtplib.SMTP_SSL(
                        self.host, self.port, timeout=30,
                        context=ssl.create_default_context(),
                    ) as server:
                        server.login(self.username, self.password)
                        server.sendmail(self.username, to_addrs, msg.as_string())
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                        server.ehlo()
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                        server.login(self.username, self.password)
                        server.sendmail(self.username, to_addrs, msg.as_string())

                _last_send_time = time.time()
                logger.info(f"邮件已发送 → {', '.join(to_addrs)} | 标的: {stock_codes}")
                return True

            except smtplib.SMTPAuthenticationError as e:
                logger.error(f"SMTP 认证失败: {e}")
                return False
            except Exception as e:
                last_error = e
                err_msg = str(e)
                if "Too many" in err_msg or "limit" in err_msg.lower():
                    wait = 30 + attempt * 30
                    logger.warning(f"触发频率限制，等待 {wait}s 重试... (attempt {attempt + 1}/3)")
                else:
                    wait = 5
                    logger.warning(f"发送失败 ({err_msg[:60]})，{wait}s 后重试 (attempt {attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(wait)

        logger.error(f"邮件发送失败 (已重试3次): {last_error}")
        _last_send_time = time.time()
        return False

    def _build_html(
        self,
        tweet_text: str,
        tweet_url: str,
        tweet_time: datetime,
        new_stocks: list,
    ) -> str:
        """构建 HTML 邮件正文"""
        time_str = tweet_time.strftime("%Y-%m-%d %H:%M:%S") if tweet_time else "未知"

        stock_items = ""
        market_emoji = {
            "A股(上海主板)": "🏛️", "A股(深圳主板)": "🏛️",
            "A股(创业板)": "🔬", "A股(科创板)": "🚀",
            "港股": "🇭🇰", "美股": "🇺🇸",
        }
        for code, market in new_stocks:
            emoji = market_emoji.get(market, "📌")
            stock_items += f'<li style="margin:6px 0;"><span style="font-size:16px;">{emoji} <strong style="color:#d4380d;">{code}</strong></span> <span style="color:#888;">({market})</span></li>\n'

        # 处理推文文本中的特殊字符
        safe_text = tweet_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.08);overflow:hidden;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1677ff,#0958d9);padding:20px 24px;color:#fff;">
    <h2 style="margin:0;font-size:20px;">🐂 白毛股神 serenity 发推提到新标的</h2>
    <p style="margin:6px 0 0;opacity:0.85;font-size:13px;">{time_str}</p>
  </div>

  <!-- Stocks -->
  <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
    <h3 style="margin:0 0 12px;color:#333;font-size:16px;">📊 发现的股票标的</h3>
    <ul style="padding-left:20px;margin:0;">
{stock_items}
    </ul>
  </div>

  <!-- Tweet -->
  <div style="padding:20px 24px;">
    <h3 style="margin:0 0 10px;color:#333;font-size:16px;">📝 推文原文</h3>
    <blockquote style="margin:0;padding:14px 18px;background:#fafafa;border-left:4px solid #1677ff;border-radius:4px;line-height:1.7;color:#555;white-space:pre-wrap;word-break:break-word;">
{safe_text}
    </blockquote>
    <p style="margin-top:14px;">
      <a href="{tweet_url}" style="color:#1677ff;text-decoration:none;font-size:13px;" target="_blank">
        🔗 查看原文 → {tweet_url}
      </a>
    </p>
  </div>

  <!-- Footer -->
  <div style="padding:14px 24px;background:#fafafa;border-top:1px solid #f0f0f0;text-align:center;font-size:12px;color:#999;">
    股票监控机器人 | 生成于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
  </div>

</div>
</body>
</html>"""
        return html


def load_recipients(filepath: str) -> List[str]:
    """从文件加载收件人邮箱列表

    支持:
      - 每行一个邮箱
      - # 开头的行作为注释
      - 空行忽略
    """
    recipients = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "@" in line:
                    recipients.append(line)
        logger.info(f"加载收件人 {len(recipients)} 个: {recipients}")
    except FileNotFoundError:
        logger.error(f"收件人文件不存在: {filepath}")
    except Exception as e:
        logger.error(f"读取收件人文件失败: {e}")
    return recipients
