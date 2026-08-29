"""talk CLI 文本状态动画回归测试。"""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent))

import talk


class TestTalkSpinner(unittest.TestCase):
    def test_format_error_uses_http_api_message(self):
        response = mock.Mock(status_code=429, reason="Too Many Requests")
        response.json.return_value = {
            "error": {"message": "rate limit exceeded"}
        }
        error = requests.exceptions.HTTPError(response=response)

        self.assertEqual(
            talk.format_error(error),
            "HTTP 429: rate limit exceeded",
        )

    def test_format_error_uses_http_reason_when_body_is_not_json(self):
        response = mock.Mock(status_code=502, reason="Bad Gateway")
        response.json.side_effect = ValueError("not json")
        error = requests.exceptions.HTTPError(response=response)

        self.assertEqual(talk.format_error(error), "HTTP 502: Bad Gateway")

    def test_format_error_keeps_only_first_network_error_line(self):
        error = requests.exceptions.ConnectionError("connection refused\ntrace")

        self.assertEqual(talk.format_error(error), "connection refused")

    def test_format_error_has_fallback_for_empty_exception(self):
        self.assertEqual(
            talk.format_error(requests.exceptions.Timeout()),
            "请求失败（无详细原因）",
        )

    def test_format_error_truncates_long_reason(self):
        error = requests.exceptions.ConnectionError("x" * 400)

        result = talk.format_error(error)

        self.assertEqual(len(result), 300)
        self.assertTrue(result.endswith("…"))

    def test_format_error_redacts_credentials(self):
        error = requests.exceptions.ConnectionError(
            "upstream rejected Bearer secret-token token=my-token"
        )

        self.assertEqual(
            talk.format_error(error),
            "upstream rejected Bearer *** token=***",
        )

    def test_spinner_is_silent_for_non_tty_stream(self):
        stream = io.StringIO()
        with talk.TextSpinner(enabled=True, stream=stream):
            pass
        self.assertEqual(stream.getvalue(), "")

    @mock.patch("requests.post")
    @mock.patch("talk.TextSpinner")
    def test_chat_wraps_model_request_in_spinner(self, spinner, post):
        response = mock.Mock()
        response.json.return_value = {
            "choices": [{"message": {
                "content": '{"tool":"answer","answer":"ok"}'
            }}]
        }
        post.return_value = response

        session = talk.TalkSession.__new__(talk.TalkSession)
        session.base = "https://example.invalid/v1"
        session.key = "test-key"
        session.model = "test-model"
        session.messages = []
        session.quiet = False
        session.ai = mock.Mock(TIMEOUT=5)
        session.ai._extract_json.return_value = {
            "tool": "answer", "answer": "ok"
        }

        result = session._chat()

        self.assertEqual(result["answer"], "ok")
        status = spinner.return_value.__enter__.return_value
        self.assertEqual(
            status.update.call_args_list,
            [mock.call("连接模型"), mock.call("分析中"),
             mock.call("整理回复")],
        )
        self.assertTrue(post.call_args.kwargs["stream"])
        spinner.assert_called_once_with(enabled=True)
        spinner.return_value.__enter__.assert_called_once()
        spinner.return_value.__exit__.assert_called_once()

    @mock.patch("requests.post")
    @mock.patch("talk.TextSpinner")
    def test_quiet_chat_disables_spinner(self, spinner, post):
        response = mock.Mock()
        response.json.return_value = {
            "choices": [{"message": {
                "content": '{"tool":"answer","answer":"ok"}'
            }}]
        }
        post.return_value = response

        session = talk.TalkSession.__new__(talk.TalkSession)
        session.base = "https://example.invalid/v1"
        session.key = "test-key"
        session.model = "test-model"
        session.messages = []
        session.quiet = True
        session.ai = mock.Mock(TIMEOUT=5)
        session.ai._extract_json.return_value = {
            "tool": "answer", "answer": "ok"
        }

        session._chat()

        spinner.assert_called_once_with(enabled=False)

    @mock.patch("talk.TextSpinner")
    def test_step_shows_reason_only_when_model_request_fails(self, spinner):
        session = talk.TalkSession.__new__(talk.TalkSession)
        session.messages = []
        session.quiet = False
        session._chat = mock.Mock(
            side_effect=requests.exceptions.ConnectionError(
                "connection refused\ninternal details"
            )
        )

        with mock.patch("builtins.print") as output:
            result = session.step("hello")

        self.assertFalse(result)
        output.assert_called_once_with("AI 请求失败: connection refused")
        spinner.return_value.__enter__.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
