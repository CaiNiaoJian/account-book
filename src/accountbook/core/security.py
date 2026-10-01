"""安全原语 —— 本地服务的访问边界（REQ-14：不上云，但也不许"邻居"偷看）。

威胁模型（务必理解，否则会误判这些代码"多余"）
------------------------------------------------
本应用会在 ``127.0.0.1`` 上开一个 HTTP 服务供界面调用。环回地址**并不等于安全**：

1. **同机其它进程**：任何本机程序（包括另一个用户会话下的程序）都能访问
   环回端口。若不鉴权，对方一个 GET 就能读走全部账本。
2. **浏览器 CSRF / DNS rebinding**：用户用浏览器打开一个恶意网页，
   该网页可以用 ``fetch('http://127.0.0.1:<port>/api/...')`` 发起请求。
   现代浏览器的 CORS 会拦住"读响应"，但**"发请求"是拦不住的**
   （简单请求仍会发出）。因此写操作必须额外校验 Origin/Referer。
3. **端口扫描 + Host 头欺骗**：攻击者把自己的域名解析到 127.0.0.1，
   让浏览器把请求发到本机服务却带着攻击者的 Host 头 —— 这叫 DNS rebinding。

对策（三重）
------------
* **随机端口**：由操作系统分配空闲端口，攻击者需先扫描（提高门槛，非根本手段）。
* **每次启动的随机令牌**：``secrets.token_urlsafe(32)``，进程退出即失效。
  以 Cookie（HttpOnly + SameSite=Strict）与 ``Authorization: Bearer`` 两种方式接受。
* **Host / Origin 白名单**：只接受 ``127.0.0.1:<实际端口>`` 与 ``localhost:<实际端口>``；
  带 Origin 头的请求其 Origin 必须命中同一白名单，否则 403。

时间比较一律使用 :func:`secrets.compare_digest`，避免通过响应时间差暴力猜测令牌。
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

__all__ = [
    "COOKIE_NAME",
    "SessionToken",
    "build_origin_whitelist",
    "constant_time_equals",
    "is_loopback_host",
    "mask_token_in_url",
    "origin_allowed",
]

_logger = logging.getLogger(__name__)

#: 令牌长度（字节）。32 字节 → 43 个 URL 安全字符，暴力破解不可行。
TOKEN_BYTES = 32

#: 会话 Cookie 名。带前缀以降低与其它本地应用撞名的概率。
COOKIE_NAME = "ab_session"

#: 允许的 Host 主机名白名单（不含端口）
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def constant_time_equals(left: str, right: str) -> bool:
    """恒定时间字符串比较，用于令牌校验。

    注意：``compare_digest`` 对**非 ASCII** 字符串会抛 TypeError，
    因此先做 UTF-8 编码再比较。
    """
    try:
        return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (TypeError, AttributeError):
        return False


@dataclass(frozen=True, slots=True)
class SessionToken:
    """一次进程生命周期的访问令牌。

    刻意设计为**不可序列化、不落盘**：令牌只存在于内存与窗口 URL 中，
    进程退出即失效，避免"上次的令牌被别的程序抄走后长期可用"。
    """

    value: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def generate(cls) -> SessionToken:
        return cls(value=secrets.token_urlsafe(TOKEN_BYTES))

    def matches(self, candidate: str | None) -> bool:
        """校验候选令牌；``None`` 或空串一律拒绝。"""
        if not candidate:
            return False
        return constant_time_equals(self.value, candidate)

    def __repr__(self) -> str:  # 防止令牌意外出现在日志/异常里
        return f"SessionToken(created_at={self.created_at.isoformat()}, value=<redacted>)"

    __str__ = __repr__


def build_origin_whitelist(port: int) -> frozenset[str]:
    """构造允许的 Origin 集合（``scheme://host:port`` 形式）。

    覆盖 http 与 https（https 用于未来可能的本地自签证书场景），
    主机名覆盖 ``127.0.0.1`` 与 ``localhost``。
    """
    origins: set[str] = set()
    for scheme in ("http", "https"):
        for host in ("127.0.0.1", "localhost"):
            origins.add(f"{scheme}://{host}:{port}")
    return frozenset(origins)


def mask_token_in_url(url: str) -> str:
    """把 URL 查询串里的令牌打码，供日志与错误提示使用。

    保留前缀与长度：排查时能确认"确实是同一个令牌"，但拿不到可用的值。
    集中在此实现，避免各处各写一份（曾出现过漏打码的路径）。
    """
    if "token=" not in url:
        return url
    head, _, token = url.partition("token=")
    # 令牌之后可能还有其它查询参数，同样需要保留
    token_part, sep, tail = token.partition("&")
    return f"{head}token={token_part[:6]}…({len(token_part)} chars){sep}{tail}"


def is_loopback_host(host_header: str | None) -> bool:
    """判断 Host 头是否指向环回地址（防 DNS rebinding）。

    接受形式：``127.0.0.1``、``127.0.0.1:8787``、``localhost:8787``、
    ``[::1]:8787``、``::1``。
    任何其它形态（含攻击者域名、``127.0.0.1.evil.com``）一律拒绝。

    实现说明：**先整体解析，再剥离端口后二次尝试**。
    顺序不能反过来 —— 裸 IPv6（``::1``）含有多个冒号，
    若先按 ``rsplit(':')`` 剥端口会得到空字符串并被误判为非法。
    """
    if not host_header:
        return False
    host = host_header.strip().lower()

    candidates: list[str] = []
    if host.startswith("["):  # IPv6 字面量：[::1] 或 [::1]:8787
        end = host.find("]")
        if end == -1:
            return False
        candidates.append(host[1:end])
    else:
        candidates.append(host)
        if ":" in host:  # 形如 host:port
            candidates.append(host.rsplit(":", 1)[0])

    for candidate in candidates:
        if not candidate:
            continue
        if candidate in _LOOPBACK_NAMES:
            return True
        try:
            # 兜底：其它形式的环回 IP（例如 127.0.0.2、0:0:0:0:0:0:0:1）
            if ipaddress.ip_address(candidate).is_loopback:
                return True
        except ValueError:
            continue
    return False


def origin_allowed(origin: str | None, whitelist: frozenset[str]) -> bool:
    """校验 Origin / Referer。

    规则：
        * 未携带 Origin（同源导航、静态资源、curl 等）→ 放行，
          因为此时请求必须已经通过令牌或 Cookie 校验；浏览器发起的跨站
          写请求**一定**会带 Origin，所以这条放行不会削弱 CSRF 防护。
        * 携带 Origin → 必须精确命中白名单。
    """
    if not origin:
        return True
    if origin in whitelist:
        return True
    # Referer 只取 scheme://host:port 部分再比对
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if not parts.scheme or not parts.netloc:
        return False
    return f"{parts.scheme}://{parts.netloc}" in whitelist
