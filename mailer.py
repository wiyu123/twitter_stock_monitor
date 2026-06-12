"""
邮件发送模块
通过 阿里云邮件推送 (SMTP) 发送 HTML 格式的股票提醒邮件，附带中文翻译。
收件人由 emails.csv 管理 (email, expire_date 两列)。
"""

import base64
import csv
import os
import smtplib
import ssl
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from datetime import datetime, date
from typing import List

logger = logging.getLogger(__name__)


def translate_text(text: str, target: str = "zh-CN") -> str:
    """用 deep-translator (GoogleTranslate) 翻译文本为中文。"""
    if not text or len(text.strip()) < 10:
        return ""
    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="auto", target=target).translate(text)
        if result and result != text:
            return result.strip()
    except Exception as e:
        logger.debug(f"翻译失败: {e}")
    return ""


class Mailer:
    """邮件发送器 (专为阿里云邮件推送优化版)"""

    def __init__(self, config: dict):
        # 阿里云邮件推送默认地址
        self.host: str = config.get("host", "smtpdm.aliyun.com")
        self.port: int = config.get("port", 465)
        self.use_ssl: bool = config.get("use_ssl", True)
        self.username: str = config.get("username", "")  # 后台创建的发信地址
        self.password: str = config.get("password", "")  # 自定义的 SMTP 密码
        self.from_name: str = config.get("from_name", "股票监控机器人")

    def send_tweet_alert(
        self,
        to_addrs: List[str],
        tweet_text: str,
        tweet_url: str,
        tweet_time: datetime,
        stocks: list,
        images: list = None,
    ) -> bool:
        """并行发送邮件给多位收件人（有/无股票标的都发）。"""
        if not to_addrs:
            logger.warning("收件人列表为空，跳过发送")
            return False

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        if stocks:
            codes = ", ".join([c for c, _ in stocks])
            subject = f"[股神监控] 新标的提醒 - {codes} - {now_str} (不要回复！)"
        else:
            subject = f"[股神监控] 新推文提醒 - {now_str} (不要回复！)"

        translation = translate_text(tweet_text)
        html = self._build_html(tweet_text, tweet_url, tweet_time, stocks, translation, images or [])

        n = len(to_addrs)
        # ── 优化：降低线程池上限。阿里云控制台 SMTP 推荐并发通常不超过 10~20 ──
        pool_size = max(1, min(n // 5 + (1 if n % 5 else 0), 15))
        logger.info(f"开始并行发送 {n} 封邮件 (线程池: {pool_size})")

        success, fail = 0, 0
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = {
                executor.submit(self._send_one, addr, subject, html, images): addr
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
                    logger.error(f"线程执行异常 ({addr}): {e}")
                    fail += 1

        logger.info(f"邮件发送完毕: 成功={success} 失败={fail}")
        return success > 0

    def _send_one(self, to_addr: str, subject: str, html: str, images: list = None) -> bool:
        """单线程：发送单封邮件（支持 MIME CID 内嵌图片，手机端正常显示）。"""
        images = images or []

        if images and any(img.get("data") for img in images):
            # ── 有 base64 图片 → multipart/related + CID ──
            msg = MIMEMultipart("related")
            msg["Subject"] = Header(subject, "utf-8")
            from_header = formataddr((self.from_name, self.username))
            msg["From"] = from_header
            msg["To"] = to_addr

            # HTML 部分
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(html, "html", "utf-8"))
            msg.attach(alt)

            # 每张图片用 CID 嵌入（HTML 中已有 cid:img0, cid:img1 ... 引用）
            for i, img in enumerate(images):
                data = img.get("data")
                if not data:
                    continue
                img_bytes = base64.b64decode(data)
                mime_img = MIMEImage(img_bytes, _subtype="jpeg")
                mime_img.add_header("Content-ID", f"<img{i}>")
                mime_img.add_header("Content-Disposition", "inline", filename=f"tweet_{i}.jpg")
                msg.attach(mime_img)
        else:
            # ── 无图片 → 纯 HTML ──
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            from_header = formataddr((self.from_name, self.username))
            msg["From"] = from_header
            msg["To"] = to_addr
            msg.attach(MIMEText(html, "html", "utf-8"))

        for attempt in range(3):
            try:
                if self.use_ssl:
                    context = ssl.create_default_context()
                    with smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context) as server:
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=15) as server:
                        server.ehlo()
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                        server.login(self.username, self.password)
                        server.sendmail(self.username, [to_addr], msg.as_string())

                logger.info(f"已发送 → {to_addr}")
                return True

            except smtplib.SMTPAuthenticationError:
                logger.error(f"SMTP 认证失败，请检查密码: {to_addr}")
                return False  # 密码错误无需重试
            except Exception as e:
                err_msg = str(e)
                # ── 优化：针对阿里云频控的指数退避重试 ──
                if "too many" in err_msg.lower() or "limit" in err_msg.lower() or "421" in err_msg:
                    wait = 5 * (attempt + 1)  # 频控时等待 5s, 10s
                else:
                    wait = 2

                logger.warning(f"发送异常 {to_addr} ({err_msg[:40]})，{wait}s 后重试 ({attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(wait)

        logger.error(f"发送失败 (已重试3次): {to_addr}")
        return False

    def _build_html(self, tweet_text: str, tweet_url: str, tweet_time: datetime,
                    stocks: list, translation: str = "", images: list = None) -> str:
        time_str = tweet_time.strftime("%Y-%m-%d %H:%M:%S") if tweet_time else "未知"

        # 标题和标的区
        if stocks:
            header_title = "🐂 白毛股神 serenity 发推提到新标的"
            stock_items = ""
            market_emoji = {
                "A股(上海主板)": "🏛️", "A股(深圳主板)": "🏛️",
                "A股(创业板)": "🔬", "A股(科创板)": "🚀",
                "港股": "🇭🇰", "美股": "🇺🇸",
            }
            for code, market in stocks:
                emoji = market_emoji.get(market, "📌")
                stock_items += (
                    f'<li style="margin:6px 0;">'
                    f'<span style="font-size:16px;">{emoji} '
                    f'<strong style="color:#d4380d;">{code}</strong></span> '
                    f'<span style="color:#888;">({market})</span></li>\n'
                )
            stock_block = f"""
  <div style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
    <h3 style="margin:0 0 12px;color:#333;font-size:16px;">📊 发现的股票标的</h3>
    <ul style="padding-left:20px;margin:0;">
{stock_items}
    </ul>
  </div>"""
        else:
            header_title = "🐂 白毛股神 serenity 发了新推文"
            stock_block = ""

        safe_text = tweet_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # 推文图片（优先用 base64 CID，手机端也能显示）
        image_block = ""
        if images:
            imgs = ""
            for i, img in enumerate(images):
                data = img.get("data")
                url = img.get("url", "")
                if data:
                    # 同时写 CID + 外链 fallback
                    imgs += (
                        f'<img src="cid:img{i}" style="max-width:100%;margin:8px 0;border-radius:8px;" '
                        f'alt="tweet image" />\n'
                    )
                elif url:
                    imgs += (
                        f'<img src="{url}" style="max-width:100%;margin:8px 0;border-radius:8px;" '
                        f'alt="tweet image" />\n'
                    )
            if imgs:
                image_block = f"""
          <div style="margin-top:12px;">
            <div style="font-size:11px;color:#999;margin-bottom:4px;">📷 推文附图</div>
            {imgs}
          </div>"""

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
    <h2 style="margin:0;font-size:20px;">{header_title}</h2>
    <p style="margin:6px 0 0;opacity:0.85;font-size:13px;">{time_str}</p>
  </div>
  {stock_block}
  <div style="padding:20px 24px;">
    <h3 style="margin:0 0 10px;color:#333;font-size:16px;">📝 推文原文</h3>
    <blockquote style="margin:0;padding:14px 18px;background:#fafafa;border-left:4px solid #1677ff;border-radius:4px;line-height:1.7;color:#555;white-space:pre-wrap;word-break:break-word;">
{safe_text}
    </blockquote>
    {image_block}
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
    """从 CSV 文件加载收件人邮箱（去重 + 过滤过期）。"""
    recipients = []
    if not os.path.exists(filepath):
        logger.error(f"收件人文件不存在: {filepath}")
        return recipients

    today = date.today()
    try:
        # utf-8-sig 能完美兼容 Windows Excel 编辑保存的 CSV 带来的 BOM 头
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                email = row[0].strip()
                if "@" not in email:
                    continue

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

    seen = set()
    unique = []
    for r in recipients:
        r_lower = r.lower()
        if r_lower not in seen:
            seen.add(r_lower)
            unique.append(r)

    logger.info(f"成功加载有效收件人 {len(unique)} 个")
    return unique
