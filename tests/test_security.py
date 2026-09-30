"""安全原语用例（REQ-14）。

这一组是本项目**最不能省**的测试：本地环回服务没有会话令牌就等于把账本
暴露给同机所有进程与任意网页。用例覆盖令牌、Host 与 Origin 三个关卡。
"""

from __future__ import annotations

import pytest

from accountbook.core.security import (
    COOKIE_NAME,
    SessionToken,
    build_origin_whitelist,
    constant_time_equals,
    is_loopback_host,
    origin_allowed,
)


class TestSessionToken:
    """会话令牌。"""

    def test_generate_is_unique_and_long_enough(self) -> None:
        """令牌必须足够长且每次不同（否则可被暴力枚举）。"""
        tokens = {SessionToken.generate().value for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(token) >= 40 for token in tokens)

    def test_matches_only_exact_value(self) -> None:
        token = SessionToken.generate()
        assert token.matches(token.value) is True
        assert token.matches(token.value + "x") is False
        assert token.matches(token.value[:-1]) is False

    @pytest.mark.parametrize("candidate", [None, "", "  ", "not-a-token"])
    def test_matches_rejects_empty_and_none(self, candidate: str | None) -> None:
        assert SessionToken.generate().matches(candidate) is False

    def test_repr_and_str_redact_value(self) -> None:
        """令牌绝不能出现在日志/异常里 —— repr 与 str 都必须打码。"""
        token = SessionToken.generate()
        for text in (repr(token), str(token)):
            assert token.value not in text
            assert "redacted" in text


class TestConstantTimeCompare:
    """恒定时间比较。"""

    def test_equal_and_unequal(self) -> None:
        assert constant_time_equals("abc", "abc") is True
        assert constant_time_equals("abc", "abd") is False
        assert constant_time_equals("", "") is True

    def test_handles_non_ascii_without_raising(self) -> None:
        """``secrets.compare_digest`` 对非 ASCII 字符串会抛错，必须被兜住。"""
        assert constant_time_equals("令牌", "令牌") is True
        assert constant_time_equals("令牌", "口令") is False


class TestLoopbackHostGuard:
    """Host 守卫（防 DNS rebinding）。"""

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.1:8787",
            "localhost",
            "localhost:8787",
            "LOCALHOST:8787",
            "[::1]:8787",
            "::1",
            "127.0.0.2:80",
        ],
    )
    def test_accepts_loopback_forms(self, host: str) -> None:
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            None,
            "",
            "evil.com",
            "evil.com:8787",
            "127.0.0.1.evil.com",  # 经典绕过写法
            "localhost.evil.com",
            "0.0.0.0:8787",
            "192.168.1.10:8787",
            "example.com:80",
        ],
    )
    def test_rejects_non_loopback(self, host: str | None) -> None:
        assert is_loopback_host(host) is False


class TestOriginWhitelist:
    """Origin / Referer 校验（防 CSRF）。"""

    def test_whitelist_covers_http_and_https(self) -> None:
        whitelist = build_origin_whitelist(8787)
        assert "http://127.0.0.1:8787" in whitelist
        assert "http://localhost:8787" in whitelist
        assert "https://127.0.0.1:8787" in whitelist
        assert "http://127.0.0.1:9999" not in whitelist

    def test_missing_origin_is_allowed(self) -> None:
        """无 Origin 的请求（同源导航、curl、探活）放行。"""
        assert origin_allowed(None, build_origin_whitelist(8787)) is True
        assert origin_allowed("", build_origin_whitelist(8787)) is True

    def test_matching_origin_allowed(self) -> None:
        whitelist = build_origin_whitelist(8787)
        assert origin_allowed("http://127.0.0.1:8787", whitelist) is True
        assert origin_allowed("http://localhost:8787", whitelist) is True

    def test_referer_full_url_is_reduced_before_compare(self) -> None:
        """Referer 是完整 URL，需要先归一化到 scheme://host:port。"""
        whitelist = build_origin_whitelist(8787)
        assert origin_allowed("http://127.0.0.1:8787/transactions?page=2", whitelist) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.com",
            "http://evil.com:8787",
            "http://127.0.0.1:9999",  # 端口不符 —— 同机其它服务
            "null",
            "file://",
        ],
    )
    def test_rejects_foreign_origins(self, origin: str) -> None:
        assert origin_allowed(origin, build_origin_whitelist(8787)) is False


def test_cookie_name_is_stable() -> None:
    """Cookie 名带应用前缀，降低与其它本地应用撞名的概率。"""
    assert COOKIE_NAME == "ab_session"
