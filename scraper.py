"""
X (Twitter) 推文抓取模块
直接调用 X.com GraphQL API，通过代理访问。
"""

import asyncio
import json
import logging
import re
import time as time_module
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 常量 ──
BEARER_TOKEN = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class TwitterScraper:
    """Twitter 推文抓取器 (基于 X GraphQL API)

    自动从 X.com 首页 JS 中提取最新的 GraphQL query ID。
    """

    # 默认 fallback query IDs
    _DEFAULT_QUERY_IDS = {
        "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
        "UserTweets": "54_zVtVXJlQtnIBrY2QSXQ",
        "UserTweetsAndReplies": "xdqXQQg4vOBF9Np6VtUsdw",
        "TweetResultByRestId": "SgZWKwvBiOKrSC0QeOGvXw",
    }

    # 缓存的 query IDs
    _query_ids_cache: dict = {}
    _query_ids_fetched = False

    def __init__(self, username: str = "", password: str = "",
                 target_user: str = "serenity", proxy: str = None):
        self.target_user = target_user
        self.proxy = proxy
        self.username = username
        self.password = password
        self._client: Optional[httpx.AsyncClient] = None
        self._guest_token: Optional[str] = None
        self._user_id: Optional[str] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                proxy=self.proxy,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=30,
                follow_redirects=True,
            )
        return self._client

    async def _refresh_query_ids(self):
        """从 X.com 首页 JS 中提取最新的 GraphQL query IDs"""
        if TwitterScraper._query_ids_fetched:
            return

        client = self._get_client()
        target_queries = {
            "UserByScreenName", "UserTweets", "UserTweetsAndReplies",
        }

        try:
            r = await client.get("https://x.com")
            js_urls = re.findall(
                r'src="(https://abs\.twimg\.com/responsive-web/client-web/[^"]+\.js)"',
                r.text,
            )
            for js_url in js_urls:
                try:
                    js_r = await client.get(js_url)
                    for qid, name in re.findall(
                        r'"queryId":"([a-zA-Z0-9_-]+)","operationName":"([^"]+)"',
                        js_r.text,
                    ):
                        if name in target_queries and name not in TwitterScraper._query_ids_cache:
                            TwitterScraper._query_ids_cache[name] = qid
                            logger.debug(f"刷新 query ID: {name} = {qid}")
                except Exception:
                    continue

            if TwitterScraper._query_ids_cache:
                TwitterScraper._query_ids_fetched = True
                logger.info(f"已获取最新 query IDs: {list(TwitterScraper._query_ids_cache.keys())}")
        except Exception as e:
            logger.warning(f"刷新 query IDs 失败: {e}，使用默认值")

    @staticmethod
    def _get_query_id(name: str) -> str:
        """获取指定 GraphQL query 的 ID (优先缓存，fallback 默认值)"""
        return TwitterScraper._query_ids_cache.get(
            name, TwitterScraper._DEFAULT_QUERY_IDS.get(name, "")
        )

    async def _api_headers(self) -> dict:
        """获取 API 请求头（含 guest token）"""
        if not self._guest_token:
            await self._refresh_guest_token()
        return {
            "Authorization": BEARER_TOKEN,
            "X-Guest-Token": self._guest_token or "",
        }

    async def _refresh_guest_token(self) -> bool:
        """刷新 guest token"""
        client = self._get_client()
        for attempt in range(3):
            try:
                r = await client.post(
                    "https://api.x.com/1.1/guest/activate.json",
                    headers={"Authorization": BEARER_TOKEN},
                )
                if r.status_code == 200:
                    self._guest_token = r.json()["guest_token"]
                    logger.debug("Guest token 已刷新")
                    return True
                else:
                    logger.warning(f"获取 guest token 失败: HTTP {r.status_code} (attempt {attempt + 1}/3)")
            except Exception as e:
                logger.warning(f"获取 guest token 异常: {e} (attempt {attempt + 1}/3)")
            if attempt < 2:
                await asyncio.sleep(2)
        logger.error("获取 guest token 失败，已重试 3 次")
        return False

    async def _get_user_id(self) -> Optional[str]:
        """通过用户名获取用户 ID"""
        if self._user_id:
            return self._user_id

        result = await self.get_user()
        if result:
            self._user_id = result.get("rest_id", "") or result.get("id_str", "")
        return self._user_id

    async def get_user(self) -> Optional[dict]:
        """获取用户完整信息 (含 pinned tweet IDs)"""
        await self._refresh_query_ids()
        client = self._get_client()
        headers = await self._api_headers()

        variables = json.dumps({"screen_name": self.target_user})
        params = {"variables": variables, "features": json.dumps({"hidden_profile_likes_enabled": True})}

        qid = self._get_query_id("UserByScreenName")
        try:
            r = await client.get(
                f"https://x.com/i/api/graphql/{qid}/UserByScreenName",
                headers=headers,
                params=params,
            )
            if r.status_code != 200:
                logger.error(f"UserByScreenName 失败: HTTP {r.status_code}")
                return None

            data = r.json()
            user_result = data.get("data", {}).get("user", {}).get("result", {})
            if user_result:
                self._user_id = user_result.get("rest_id", "")
                logger.info(f"用户 @{self.target_user} ID={self._user_id}")
                # 提取 pinned tweet IDs
                legacy = user_result.get("legacy", {})
                pinned = legacy.get("pinned_tweet_ids_str", [])
                user_result["pinned_tweet_ids_str"] = pinned
                return user_result
            return None

        except Exception as e:
            logger.error(f"获取用户信息异常: {e}")
            return None

    async def get_recent_tweets(self, count: int = 10,
                                 include_replies: bool = True) -> list[dict]:
        """获取目标用户最近 count 条推文（含回复）

        Args:
            count: 获取数量
            include_replies: 是否包含回复推文（默认 True）
                           注意：X 的 UserTweetsAndReplies 可能需要登录，
                           失败时自动降级为 UserTweets（仅主推文）

        Returns:
            list[dict]: 推文列表，每项包含 id, text, created_at, url
        """
        user_id = await self._get_user_id()
        if not user_id:
            logger.error(f"无法找到用户 @{self.target_user}")
            return []

        await self._refresh_query_ids()
        client = self._get_client()
        headers = await self._api_headers()

        # 尝试顺序：UserTweetsAndReplies → UserTweets
        queries_to_try = []
        if include_replies:
            queries_to_try.append("UserTweetsAndReplies")
        queries_to_try.append("UserTweets")

        tweets = []
        for query_name in queries_to_try:
            qid = self._get_query_id(query_name)
            if not qid:
                logger.warning(f"缺少 {query_name} 的 query ID，跳过")
                continue

            variables = json.dumps({
                "userId": user_id,
                "count": min(count, 40),
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            })
            params = {
                "variables": variables,
                "features": json.dumps(FEATURES),
            }

            try:
                r = await client.get(
                    f"https://x.com/i/api/graphql/{qid}/{query_name}",
                    headers=headers,
                    params=params,
                )
                if r.status_code == 401:
                    self._guest_token = None
                    headers = await self._api_headers()
                    r = await client.get(
                        f"https://x.com/i/api/graphql/{qid}/{query_name}",
                        headers=headers,
                        params=params,
                    )

                if r.status_code == 200:
                    data = r.json()
                    tweets = self._parse_timeline(data)
                    if tweets:
                        logger.info(f"通过 {query_name} 获取到 {len(tweets)} 条推文")
                        break
                    else:
                        logger.debug(f"{query_name} 返回 0 条推文")
                elif r.status_code == 404:
                    logger.debug(f"{query_name} 不可用 (404)，尝试下一个")
                else:
                    logger.warning(f"{query_name} 失败: HTTP {r.status_code}")

            except Exception as e:
                logger.warning(f"{query_name} 请求异常: {e}")

        if tweets:
            if len(tweets) > count:
                tweets.sort(key=lambda t: int(t.get("id", "0")), reverse=True)
                tweets = tweets[:count]
            logger.info(f"获取到 @{self.target_user} 最近 {len(tweets)} 条推文")

            # ── 补充: 检查 pinned tweet 附近的新推文 ──
            # UserTweets API 有 CDN 缓存，可能延迟数小时到数天。
            # 但 pinned tweet (置顶推文) 可通过 UserByScreenName 实时获取，
            # 如果 pinned tweet 变了说明有新内容发布。
            speculative = await self._check_pinned_tweets(headers)
            if speculative:
                existing_ids = {t["id"] for t in tweets}
                for st in speculative:
                    if st["id"] not in existing_ids:
                        tweets.append(st)
                        existing_ids.add(st["id"])
                tweets.sort(key=lambda t: int(t.get("id", "0")), reverse=True)
                tweets = tweets[:count]
                logger.info(f"合并后共 {len(tweets)} 条推文 (含 {len(speculative)} 条实时补充)")
        else:
            logger.warning(f"未获取到 @{self.target_user} 的推文")
        return tweets

    async def _fetch_tweet_by_id(self, tweet_id: str) -> Optional[dict]:
        """通过 TweetResultByRestId 获取单条推文"""
        client = self._get_client()
        headers = await self._api_headers()
        qid = self._get_query_id("TweetResultByRestId")
        if not qid:
            return None

        variables = json.dumps({
            "tweetId": tweet_id,
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": True,
            "withV2Timeline": True,
        })
        params = {"variables": variables, "features": json.dumps(FEATURES)}

        try:
            r = await client.get(
                f"https://x.com/i/api/graphql/{qid}/TweetResultByRestId",
                headers=headers,
                params=params,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            result = data.get("data", {}).get("tweetResult", {}).get("result", {})
            if result.get("__typename") != "Tweet":
                return None
            tweet = self._parse_tweet_result(result)
            if tweet:
                # 只返回目标用户的推文
                user_sn = self._extract_screen_name(result)
                if user_sn and user_sn.lower() == self.target_user.lower():
                    return tweet
            return None
        except Exception:
            return None

    @staticmethod
    def _extract_screen_name(tweet_result: dict) -> Optional[str]:
        """从 tweet result 中提取用户名"""
        try:
            return (
                tweet_result.get("core", {})
                .get("user_results", {})
                .get("result", {})
                .get("legacy", {})
                .get("screen_name", "")
            )
        except Exception:
            return None

    async def _check_pinned_tweets(self, headers: dict) -> list[dict]:
        """检查 pinned tweet 附近是否有新推文

        策略: 获取当前 pinned tweet ID，如果和上次不同，
        尝试用已知的 worker ID 范围探测。
        """
        user = await self.get_user()
        if not user:
            return []

        try:
            pinned_ids = user.get("pinned_tweet_ids_str", [])
        except Exception:
            return []

        if not pinned_ids:
            return []

        latest_pinned = pinned_ids[0]
        if not hasattr(self, "_last_pinned_id"):
            self._last_pinned_id = latest_pinned

        found = []

        # 如果 pinned tweet 变了，在它附近探测新推文
        if latest_pinned != self._last_pinned_id:
            logger.info(f"Pinned tweet 已变化: {self._last_pinned_id} → {latest_pinned}")
            self._last_pinned_id = latest_pinned

            # 探测新 pinned tweet 本身
            tweet = await self._fetch_tweet_by_id(latest_pinned)
            if tweet:
                found.append(tweet)

        # 每个轮询周期也尝试探测 pinned ID 上方的新推文
        # （即使 pinned 没变，也可能有新推文）
        pinned_int = int(latest_pinned)
        pinned_ts = pinned_int >> 22
        pinned_seq = pinned_int & 0xFFF
        pinned_worker = (pinned_int >> 12) & 0x3FF

        # 尝试时间偏移 + 已知 worker/seq 组合
        # 覆盖从 pinned 时间到"现在"的范围
        now_ts = int(time_module.time() * 1000) - 1288834974657  # Twitter epoch

        # 只探测几个代表性组合
        workers_to_try = [pinned_worker, 427, 436]  # 已知的 worker
        seqs_to_try = [pinned_seq, 30, 38, 0, 1024, 2048, 3072]
        ts_offsets = [0, 3600000, 7200000, 43200000, 86400000, 172800000, 345600000]
        # 0s, 1h, 2h, 12h, 24h, 48h, 96h

        max_checks = 12  # 每轮最多探测 12 个 ID
        checked = 0

        for ts_off in ts_offsets:
            target_ts = now_ts - ts_off
            if target_ts <= pinned_ts:
                continue
            for w in workers_to_try:
                for s in seqs_to_try:
                    if checked >= max_checks:
                        return found
                    lower = (w << 12) | s
                    est_id = str((target_ts << 22) | lower)
                    if int(est_id) <= pinned_int:
                        continue
                    tweet = await self._fetch_tweet_by_id(est_id)
                    checked += 1
                    if tweet:
                        found.append(tweet)
                        # 如果找到，更新 pinned_int 避免重复探测同一范围
                        pinned_int = max(pinned_int, int(tweet["id"]))

        return found

    def _parse_timeline(self, data: dict) -> list[dict]:
        """解析 GraphQL UserTweets 返回的时间线"""
        results = []

        try:
            timeline = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
            )
            instructions = timeline.get("instructions", [])

            for ins in instructions:
                if ins.get("type") != "TimelineAddEntries":
                    continue
                for entry in ins.get("entries", []):
                    content = entry.get("content", {})
                    if content.get("entryType") != "TimelineTimelineItem":
                        continue

                    item_content = content.get("itemContent", {})
                    tweet_result = (
                        item_content.get("tweet_results", {}).get("result", {})
                    )
                    if not tweet_result or tweet_result.get("__typename") != "Tweet":
                        continue

                    tweet = self._parse_tweet_result(tweet_result)
                    if tweet:
                        results.append(tweet)

        except (KeyError, TypeError, AttributeError) as e:
            logger.error(f"解析时间线异常: {e}")

        return results

    def _parse_tweet_result(self, result: dict) -> Optional[dict]:
        """从单个 tweet result 中提取信息"""
        try:
            tid = result.get("rest_id", "")
            if not tid:
                return None

            legacy = result.get("legacy", {})
            text = legacy.get("full_text", "")
            created_str = legacy.get("created_at", "")

            if not text:
                return None

            created_at = _parse_twitter_time(created_str)

            # 获取用户名用于 URL
            core = result.get("core", {})
            user_results = core.get("user_results", {}).get("result", {})
            screen_name = user_results.get("legacy", {}).get(
                "screen_name", self.target_user
            )

            return {
                "id": tid,
                "text": text,
                "created_at": created_at,
                "url": f"https://x.com/{screen_name}/status/{tid}",
            }
        except Exception as e:
            logger.debug(f"解析 tweet result 失败: {e}")
            return None

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None


def _parse_twitter_time(created_str: str) -> Optional[datetime]:
    """解析 Twitter 时间格式 "Wed Jun 08 10:00:00 +0000 2026" """
    if not created_str:
        return None
    try:
        return datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    return None
