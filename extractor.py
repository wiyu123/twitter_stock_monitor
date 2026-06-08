"""
股票代码提取模块
从推文文本中提取 A股、港股、美股 代码。
"""

import re
import logging
from typing import Set

logger = logging.getLogger(__name__)

# ── 黑名单: 常见非股票的大写缩写 ──
BLACKLIST: Set[str] = {
    # ── 通用缩写/单词 ──
    "A", "I", "OK", "CEO", "CFO", "CTO", "IPO", "ETF", "GDP",
    "AI", "API", "APP", "USA", "UK", "EU", "US", "USD", "CNY",
    "HKD", "RMB", "IT", "TV", "PC", "VR", "AR", "MR", "PM",
    "AM", "PM2", "CEO", "COO", "CMO", "CPO", "IS", "TO", "IF",
    "BE", "SO", "GO", "NO", "IN", "ON", "AT", "BY", "AS", "AN",
    "DO", "WE", "HE", "HI", "MY", "ME", "WE", "OF", "UP", "OR",
    # ── 财经术语 ──
    "EPS", "PE", "PB", "ROE", "ROA", "EBITDA", "YOY", "QOQ",
    "FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "YTD", "MTD",
    "CPI", "PPI", "FED", "PBOC", "ECB", "BOJ", "BOE",
    "MACD", "RSI", "KDJ", "SMA", "EMA", "MA",
    "VIX", "FCF", "EBIT", "CAGR",
    # ── X/Twitter 常见 ──
    "DM", "RT", "LOL", "OMG", "WTF", "BTW", "FYI", "IMO",
    "TBH", "IDK", "NVM", "IRL", "TBD", "TBA", "ETA",
    "TLDR", "NFA", "AMA", "DYOR", "OFC",
    # ── 市场术语 ──
    "RE", "AND", "THE", "FOR", "ALL", "NEW", "BIG",
    "TOP", "HOT", "BUY", "SELL", "HOLD", "LONG", "SHORT",
    "CALL", "PUT", "SPAC", "OTC", "NYSE", "NASDAQ",
    "SSE", "SZSE", "HKEX", "SEHK",
    "DD", "YOLO", "ATH", "ATL", "FOMO", "FUD",
    "DCA", "LTV", "ROI", "APY", "APR",
    "KYC", "AML", "KOL", "DAO", "DEFI", "CEX", "DEX",
    "NFT", "WEB3", "PFP", "GM", "GN", "WAGMI", "NGMI",
    # ── 常见英文单词 (易误判为美股) ──
    "THIS", "THAT", "THAN", "THEN", "THEM", "THEY", "HERE", "THERE",
    "LEARN", "PORT", "NEVER", "USE", "YOU", "HAVE", "LARGE", "MORE",
    "DEAL", "END", "TAKES", "SHAPE", "DON", "NEED", "SAY",
    "HIGH", "LOW", "MINT", "MEGA", "MOAT", "LIFT", "FLY",
    "LOT", "EST", "LEGAL", "OPEN", "SEC", "CAP",
    "JUST", "LIKE", "MUCH", "VERY", "WELL", "ONLY", "EVEN",
    "ALSO", "MANY", "SOME", "EACH", "WITH", "FROM", "WILL",
    "MAKE", "MADE", "KNOW", "THINK", "LOOK", "SEEN", "SEE",
    "GOOD", "GREAT", "REAL", "NEXT", "LAST", "SAME", "SURE",
    "KEEP", "KEPT", "FIND", "FOUND", "GIVE", "GAVE", "TAKE",
    "TOOK", "COME", "CAME", "WENT", "TELL", "SAID", "HELP",
    # ── 行业术语/非股票 ──
    "GPU", "AWS", "GPT", "HPC", "BTC", "ETH", "SOL", "DOT",
    "CAD", "MM", "DC", "JV", "FS", "BS", "ER", "PT", "MC",
    "ARR", "AX", "GW", "SG", "RR", "SEA", "SK", "FI", "LGN",
    "LYC", "TA", "ERT", "ATM", "CHEAP", "BULL", "BEAR",
    "MINT", "LIFT", "CAP", "MOAT", "MEGA", "DON", "IV",
    # ── 常见月份/星期缩写 ──
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
    "SEP", "OCT", "NOV", "DEC",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
}

# ── 正则规则 ──
# A股：00xxxx (深圳主板) / 30xxxx (创业板) / 60xxxx (上海主板) / 68xxxx (科创板)
# 必须独立出现（前后不是数字）
_A_SHARE_RE = re.compile(r'(?<!\d)(00\d{4}|30\d{4}|60\d{4}|68\d{4})(?!\d)')

# 港股：0xxxx (5位数字，0开头)
_HK_RE = re.compile(r'(?<!\d)(0\d{4})(?!\d)')

# 美股：$TICKER 格式优先，其次是大写 2-5 字母的 ticker
_US_DOLLAR_RE = re.compile(r'\$([A-Z]{2,5})\b')
_US_PLAIN_RE = re.compile(r'(?<![A-Z\$])\b([A-Z]{2,5})\b(?![A-Z])')


def classify_market(code: str) -> str:
    """根据代码判断市场"""
    if re.match(r'^(00|30|60|68)\d{4}$', code):
        if code.startswith("68"):
            return "A股(科创板)"
        elif code.startswith("30"):
            return "A股(创业板)"
        elif code.startswith("60"):
            return "A股(上海主板)"
        else:
            return "A股(深圳主板)"
    if re.match(r'^0\d{4}$', code):
        return "港股"
    return "美股"


def extract_stocks(text: str) -> list[tuple[str, str]]:
    """从文本中提取股票代码

    Args:
        text: 推文文本

    Returns:
        list[(code, market)]: 股票代码和市场分类列表，如 [("600519", "A股(上海主板)"), ("TSLA", "美股")]
    """
    found: dict[str, str] = {}  # code -> market, 用 dict 去重

    # 1) A股
    for m in _A_SHARE_RE.finditer(text):
        code = m.group(0)
        found[code] = classify_market(code)

    # 2) 港股（注意：A股已经匹配的 0xxxx 不会被港股的 0xxxx 覆盖，因为港股正则是5位）
    for m in _HK_RE.finditer(text):
        code = m.group(0)
        # A股已匹配的不重复
        if code not in found:
            found[code] = classify_market(code)

    # 3) 美股 $TICKER (优先)
    dollar_tickers = set()
    for m in _US_DOLLAR_RE.finditer(text):
        ticker = m.group(1)
        if ticker.upper() not in BLACKLIST:
            dollar_tickers.add(ticker.upper())
            found[ticker.upper()] = "美股"

    # 4) 美股普通大写词
    for m in _US_PLAIN_RE.finditer(text):
        ticker = m.group(1).upper()
        if ticker in BLACKLIST:
            continue
        if ticker in dollar_tickers:
            continue
        # 过滤纯数字 (比如年份 2026)
        if ticker.isdigit():
            continue
        found[ticker] = "美股"

    # 按文本中出现顺序排序
    results = []
    text_upper = text.upper()
    for code, market in found.items():
        pos = text_upper.find(code.upper())
        results.append((pos if pos >= 0 else 9999, code, market))

    results.sort(key=lambda x: x[0])
    return [(code, market) for _, code, market in results]


if __name__ == "__main__":
    # 快速测试
    test_texts = [
        "今天看好 600519 和 000858，还有港股 00700 也不错",
        "I'm bullish on $TSLA and $NVDA right now",
        "茅台 600519 五粮液 000858 美股 AAPL 港股 09988",
        "关注 AI ETF 和 IPO 市场，CEO 说了很多",
        "$AAPL $GOOGL 科技股 opportunity",
        "6位 6005191 不是代码，5位 00700 港股和 A 股 603259",
    ]
    for t in test_texts:
        stocks = extract_stocks(t)
        print(f"文本: {t}")
        print(f"  提取: {stocks}")
        print()
