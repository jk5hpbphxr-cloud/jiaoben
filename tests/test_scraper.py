"""
单元测试：测试代码逻辑，不发真实网络请求。
- 对 parse_html / extract_links：直接传 HTML 字符串，简单直接。
- 对 fetch_page：用 unittest.mock 模拟 requests.get，
  不依赖网络，测试环境完全可控。
"""

import unittest
from unittest.mock import patch, MagicMock

from scraper.main import parse_html, extract_links, fetch_page


class TestParseHtml(unittest.TestCase):
    def test_returns_h1_text(self):
        html = "<html><body><h1>标题内容</h1></body></html>"
        self.assertEqual(parse_html(html), "标题内容")

    def test_returns_empty_when_no_h1(self):
        html = "<html><body><p>没有标题</p></body></html>"
        self.assertEqual(parse_html(html), "")

    def test_strips_whitespace(self):
        html = "<html><body><h1>  空格  </h1></body></html>"
        self.assertEqual(parse_html(html), "空格")


class TestExtractLinks(unittest.TestCase):
    def test_extracts_multiple_links(self):
        html = '<a href="/page1">一</a><a href="/page2">二</a>'
        self.assertEqual(extract_links(html), ["/page1", "/page2"])

    def test_ignores_anchors_without_href(self):
        html = '<a name="top">锚点</a><a href="/real">链接</a>'
        self.assertEqual(extract_links(html), ["/real"])

    def test_returns_empty_list_when_no_links(self):
        html = "<p>没有链接</p>"
        self.assertEqual(extract_links(html), [])


class TestFetchPage(unittest.TestCase):
    @patch("scraper.main.requests.get")   # 拦截真实网络请求
    def test_returns_html_on_success(self, mock_get):
        # 模拟服务器返回 200 + HTML 内容
        mock_response = MagicMock()
        mock_response.text = "<html><h1>Mock 页面</h1></html>"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = fetch_page("https://example.com")
        self.assertEqual(result, "<html><h1>Mock 页面</h1></html>")
        mock_get.assert_called_once_with("https://example.com", timeout=10)

    @patch("scraper.main.requests.get")
    def test_raises_on_http_error(self, mock_get):
        # 模拟服务器返回 404
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = mock_response

        with self.assertRaises(Exception):
            fetch_page("https://example.com/404")


if __name__ == "__main__":
    unittest.main()
