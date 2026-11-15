"""评论区热评：链接识别、点赞排序、分发与卡片渲染。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lingyu.bot import comments, douyin, tiktok
from lingyu.bot.comments import Comment, CommentDigest
from lingyu.bot.config import BotConfig


class TikTokLinkTests(unittest.TestCase):
    def test_is_tiktok_url(self):
        for url in (
            "https://www.tiktok.com/@manneeenn.09/video/7706530150988500242",
            "https://tiktok.com/@user/photo/7706530150988500242",
            "https://vm.tiktok.com/ZMabc123/",
        ):
            self.assertTrue(tiktok.is_tiktok_url(url), url)
        self.assertFalse(tiktok.is_tiktok_url("https://www.douyin.com/video/1"))
        self.assertFalse(tiktok.is_tiktok_url(""))

    def test_extract_ids(self):
        url = "https://www.tiktok.com/@manneeenn.09/video/7706530150988500242?is_copy_url=1"
        self.assertEqual(tiktok.extract_video_id(url), "7706530150988500242")
        self.assertEqual(tiktok.extract_username(url), "manneeenn.09")
        # 短链里没有作品 ID，得先展开（由引擎负责）
        self.assertEqual(tiktok.extract_video_id("https://vm.tiktok.com/ZMabc123/"), "")

    def test_parse_likes(self):
        self.assertEqual(tiktok.parse_likes("8989"), 8989)
        self.assertEqual(tiktok.parse_likes("1.2K"), 1200)
        self.assertEqual(tiktok.parse_likes("1.2万"), 12000)
        self.assertEqual(tiktok.parse_likes("2,345"), 2345)
        self.assertEqual(tiktok.parse_likes(""), 0)
        self.assertEqual(tiktok.parse_likes("赞"), 0)

    def test_parse_rows_reads_time_likes_and_replies(self):
        """点赞数与时间都没有 data-e2e，只能从容器文本的行里读（实测结构）。"""
        when, likes, replies = tiktok._parse_rows(
            ["😇", "球が多くて分からない", "4 天前", "回复", "9086", "查看 12 条回复"]
        )
        self.assertEqual(when, "4 天前")
        self.assertEqual(likes, 9086)
        self.assertEqual(replies, 12)

        when, likes, replies = tiktok._parse_rows(
            ["someuser", "hello world", "2024-05-05", "Reply", "1.2K"]
        )
        self.assertEqual(when, "2024-05-05")
        self.assertEqual(likes, 1200)
        self.assertEqual(replies, 0)

        # 只有「-」表示 0 赞；空行直接忽略
        self.assertEqual(
            tiktok._parse_rows(["u", "", "23 分钟前", "回复", "-"]), ("23 分钟前", 0, 0)
        )
        self.assertEqual(tiktok._parse_rows([]), ("", 0, 0))

    def test_parse_rows_accepts_month_day_time(self):
        """桌面非英文界面把时间写成「10-2」（省略年份），不能漏掉点赞数与回复数。"""
        when, likes, replies = tiktok._parse_rows(
            ["y", "", "这音乐太带派了😭", "10-2", "", "回复", "", "173", "查看 13 条回复"]
        )
        self.assertEqual(when, "10-2")
        self.assertEqual(likes, 173)
        self.assertEqual(replies, 13)

    def test_to_comment(self):
        item = {
            "user": "うえさま",
            "text": "球が多くて分からない",
            "rows": ["うえさま", "球が多くて分からない", "4 天前", "回复", "9086", "查看 12 条回复"],
        }
        comment = tiktok._to_comment(item)
        self.assertIsNotNone(comment)
        assert comment is not None
        self.assertEqual(comment.author, "うえさま")
        self.assertEqual(comment.text, "球が多くて分からない")
        self.assertEqual(comment.likes, 9086)
        self.assertEqual(comment.time_text, "4 天前")
        self.assertEqual(comment.replies, 12)
        # 纯 emoji / 纯不可见字符的昵称在实际字体里画不出来（就是豆腐块），按匿名处理
        self.assertEqual(
            tiktok._to_comment({"user": "😇", "text": "hi", "rows": []}).author,
            "TikTok 用户",
        )
        self.assertEqual(
            tiktok._to_comment({"user": "￼ ￼", "text": "hi", "rows": []}).author,
            "TikTok 用户",
        )
        # 没有正文（比如只有贴纸）的条目直接丢掉
        self.assertIsNone(tiktok._to_comment({"user": "u", "text": "", "rows": []}))


class DouyinCommentTests(unittest.TestCase):
    def test_parse_comments_sorts_by_likes(self):
        """接口返回顺序不是点赞顺序，「热评」必须自己排。"""
        payload = {
            "status_code": 0,
            "comments": [
                {"text": "第一条", "digg_count": 167, "user": {"nickname": "A"}},
                {"text": "第二条", "digg_count": 1, "user": {"nickname": "B"}},
                {"text": "第三条", "digg_count": 418, "user": {"nickname": "C"}},
                # 空正文与脏数据直接丢弃，不占「前十条」的名额
                {"text": "   ", "digg_count": 999, "user": {"nickname": "D"}},
                {"text": "第四条", "digg_count": "7", "user": {}},
                "not-a-dict",
            ],
        }
        result = douyin.parse_comments(payload)
        self.assertEqual([item.text for item in result], ["第三条", "第一条", "第四条", "第二条"])
        self.assertEqual(result[0].author, "C")
        self.assertEqual(result[0].likes, 418)
        self.assertEqual(result[2].likes, 7)

    def test_comment_path_contains_aweme_and_paging(self):
        path = douyin._comment_path("7596543450267391625", 20)
        self.assertTrue(path.startswith("/aweme/v1/web/comment/list/?"))
        self.assertIn("aweme_id=7596543450267391625", path)
        self.assertIn("count=20", path)
        self.assertIn("cursor=0", path)


class ForUrlTests(unittest.TestCase):
    def test_douyin_dispatch(self):
        video = douyin.DouyinVideo(
            aweme_id="1", ok=True, title="标题", uploader="作者"
        )
        batch = douyin.DouyinComments(
            aweme_id="1", comments=[Comment("A", "热评正文", 99)]
        )
        with mock.patch.object(douyin, "video_for", return_value=video), \
             mock.patch.object(douyin, "comments_for", return_value=batch):
            digest = comments.for_url("https://www.douyin.com/video/7596543450267391625")

        self.assertTrue(digest.ok)
        self.assertEqual(digest.platform, "抖音")
        self.assertEqual(digest.title, "标题")
        self.assertEqual(digest.author, "作者")
        self.assertEqual(digest.comments[0].text, "热评正文")

    def test_tiktok_dispatch(self):
        batch = tiktok.TikTokComments(
            video_id="7706530150988500242",
            comments=[Comment("U", "nice", 9)],
        )
        with mock.patch.object(
            tiktok, "fetch_comments_via_browser", return_value=batch
        ):
            digest = comments.for_url(
                "https://www.tiktok.com/@u/video/7706530150988500242"
            )

        self.assertTrue(digest.ok)
        self.assertEqual(digest.platform, "TikTok")
        self.assertEqual(digest.card_id, "7706530150988500242")

    def test_failure_is_reported(self):
        batch = douyin.DouyinComments(aweme_id="1234567890", error="评论接口拒绝请求")
        with mock.patch.object(
            douyin,
            "video_for",
            return_value=douyin.DouyinVideo(aweme_id="1234567890"),
        ), mock.patch.object(douyin, "comments_for", return_value=batch):
            digest = comments.for_url("https://www.douyin.com/video/1234567890")
        self.assertFalse(digest.ok)
        self.assertIn("评论接口", digest.error)

    def test_unsupported_link(self):
        digest = comments.for_url("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertFalse(digest.ok)
        self.assertIn("不支持", digest.error)


class RenderTests(unittest.TestCase):
    def test_render_writes_png(self):
        digest = CommentDigest(
            platform="抖音",
            url="https://www.douyin.com/video/1",
            title="这是一个稍微长一点的视频标题，用来验证标题换行",
            author="某位作者",
            card_id="1",
            comments=[
                Comment("点赞最多的人", "很长的评论内容。" * 30, 16_700),
                Comment("第二个人", "短评", 3),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = comments.render_card(digest, tmp)

            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(Path(path).is_file())
            self.assertEqual(Path(path).name, "comments-1.png")
            self.assertGreater(Path(path).stat().st_size, 1000)

    def test_render_empty_comments_still_produces_card(self):
        digest = CommentDigest(platform="TikTok", url="https://tiktok.com/x", card_id="x")
        with tempfile.TemporaryDirectory() as tmp:
            path = comments.render_card(digest, tmp)
        self.assertIsNotNone(path)


class ConfigTests(unittest.TestCase):
    def test_comment_keyword_list(self):
        cfg = BotConfig(link_comments_keywords="热评，评论区; 神评")
        self.assertEqual(cfg.comment_keyword_list(), ["热评", "评论区", "神评"])
        self.assertEqual(BotConfig(link_comments_keywords="").comment_keyword_list(), [])


if __name__ == "__main__":
    unittest.main()
