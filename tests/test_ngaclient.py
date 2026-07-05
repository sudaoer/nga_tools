import unittest

from nga_tools.ngaclient.client import _parse_pid_redirect_location


class PidRedirectLocationTest(unittest.TestCase):
    def test_parses_relative_location(self) -> None:
        self.assertEqual(
            _parse_pid_redirect_location(
                "/read.php?tid=45638329&page=5066#pid870764464Anchor"
            ),
            {"tid": 45638329, "page": 5066},
        )

    def test_parses_absolute_location(self) -> None:
        self.assertEqual(
            _parse_pid_redirect_location(
                "https://bbs.nga.cn/read.php?tid=39385522&page=799#pid862245775Anchor"
            ),
            {"tid": 39385522, "page": 799},
        )

    def test_rejects_missing_or_invalid_fields(self) -> None:
        self.assertIsNone(_parse_pid_redirect_location("/read.php?tid=45638329"))
        self.assertIsNone(
            _parse_pid_redirect_location("/read.php?tid=45638329&page=abc")
        )
        self.assertIsNone(_parse_pid_redirect_location("/read.php?tid=0&page=1"))


if __name__ == "__main__":
    unittest.main()
