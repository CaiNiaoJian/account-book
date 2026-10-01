"""FastAPI 应用装配 —— 同源服务 + 访问守卫 + 前端静态资源托管。

一次请求要穿过四道关卡（顺序即实现顺序）
----------------------------------------
1. **Host 校验**：拒绝非环回 Host 头（防 DNS rebinding）。
2. **Origin 校验**：带 Origin/Referer 的请求必须命中本端口白名单（防 CSRF）。
3. **令牌校验**：受保护路径必须携带有效会话令牌
   （``Authorization: Bearer`` / ``ab_session`` Cookie / 首次导航的 ``?token=``）。
4. **静态资源**：仅托管 ``web_dist`` 之内的文件，并做路径穿越防护。

"受保护路径"的定义（刻意保持简单且可解释）
    * ``/api/**``                     —— 业务接口，必然受保护
    * ``/`` 与 ``/index.html``        —— 页面入口，其中注入了会话令牌，必须受保护
    * 任何 **无扩展名** 的路径          —— SPA 深链（如 ``/transactions``），同样返回 index.html
    * 其余带扩展名的静态资源（.js/.css/.woff2/.svg…）—— 允许匿名获取。
      理由：它们只是我们自己的前端代码，不含任何用户数据；
      放行可以彻底避免"Cookie 未生效 → 白屏"这类脆弱故障。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import APP_ID, APP_NAME, APP_NAME_EN, BUILD_PHASE, __version__
from ..core.security import COOKIE_NAME, is_loopback_host, origin_allowed
from .deps import register_domain_error_handler
from .routes import accounts as accounts_routes
from .routes import assets as assets_routes
from .routes import calendar as calendar_routes
from .routes import categories as categories_routes
from .routes import kline as kline_routes
from .routes import meta as meta_routes
from .routes import planning as planning_routes
from .routes import stats as stats_routes
from .routes import system as system_routes
from .routes import taxonomy as taxonomy_routes
from .routes import templates as templates_routes
from .routes import transactions as transactions_routes
from .routes import trash as trash_routes
from .state import AppContext, context_of

__all__ = ["U8JSONResponse", "create_app"]


class U8JSONResponse(JSONResponse):
    """显式声明 UTF-8 的 JSON 响应。

    背景：FastAPI 默认只发 ``application/json``（不带 charset）。
    按 RFC 8259 这应当被理解为 UTF-8，但 Windows PowerShell 5.1 的
    ``Invoke-RestMethod`` 以及部分老库会退回 Latin-1 解码 —— 实测结果是
    所有中文变成乱码，"按分类名查找"这类操作全部落空，
    看起来像接口返回了错数据。显式声明 charset 成本为零，却消除一整类故障。
    """

    media_type = "application/json; charset=utf-8"


_logger = logging.getLogger(__name__)

#: 完全公开的路径（无需令牌）。``/health`` 供启动自检与外部探活使用，
#: 只回一个极小的固定响应，不含任何环境细节。
PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})

#: 内容安全策略（仅随 HTML 响应下发，不影响静态资源与开发服务器）。
#:
#: 取舍说明：
#:   * ``script-src`` 必须允许 ``'unsafe-inline'``，因为启动引导数据
#:     （含会话令牌）是以内联脚本注入的。这是**唯一**的内联脚本来源，
#:     且页面本身已受令牌与环回双重保护，因此可接受。
#:   * ``connect-src 'self'``：禁止页面把数据发往任何外部地址 ——
#:     这是"不上云"在网络层面的又一道技术保障，而不仅是口头承诺。
#:   * ``frame-ancestors 'none'`` + ``X-Frame-Options``：防止被嵌套钓鱼。
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

#: 随每个响应下发的安全响应头
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # 本地应用不需要任何浏览器特性（摄像头、定位、剪贴板读取…）
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}

#: 未构建前端时的兜底页面 —— 让开发者一眼看懂该做什么，而不是面对白屏。
_FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AccountBook · 前端尚未构建</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font: 15px/1.7 -apple-system, "Segoe UI Variable", "PingFang SC", "Microsoft YaHei", sans-serif;
         background:#f5f5f7; color:#1d1d1f; }
  @media (prefers-color-scheme: dark) { body { background:#1c1c1e; color:#f5f5f7; } }
  .card { max-width: 620px; padding: 32px 36px; border-radius: 20px; background: rgba(255,255,255,.72);
          box-shadow: 0 12px 40px rgba(0,0,0,.12); backdrop-filter: blur(20px); }
  @media (prefers-color-scheme: dark) { .card { background: rgba(44,44,46,.72); } }
  code { background: rgba(120,120,128,.16); padding: 2px 6px; border-radius: 6px; font-size: 13px; }
  h1 { font-size: 19px; margin: 0 0 12px; font-weight: 600; letter-spacing: -0.01em; }
  p { margin: 8px 0; color: #6e6e73; }
  @media (prefers-color-scheme: dark) { p { color:#98989d; } }
</style></head>
<body><div class="card">
  <h1>后端已就绪，前端尚未构建</h1>
  <p>API 服务运行正常（版本 %VERSION%，阶段 %PHASE%）。</p>
  <p>请在仓库根目录执行前端构建：<br><code>scripts\\build_frontend.ps1</code></p>
  <p>随后重新启动程序即可看到界面。</p>
</div></body></html>
"""


def create_app(ctx: AppContext) -> FastAPI:
    """构造 FastAPI 应用。

    参数
    ----
    ctx:
        应用上下文（路径、配置、令牌）。所有路由通过 ``request.app.state.ctx`` 取用。
    """
    app = FastAPI(
        title=f"{APP_NAME_EN} Local API",
        version=__version__,
        # 本地应用的接口文档默认关闭：减少攻击面，也避免令牌通过文档页泄露。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # 所有 JSON 响应显式声明 UTF-8。
        # 背景：FastAPI 默认只发 ``application/json``（不带 charset），
        # 而按 RFC 只应视为 UTF-8；但 Windows PowerShell 5.1 的
        # Invoke-RestMethod / 部分老库会退回 Latin-1 解码，于是所有中文变成乱码
        # （实测：中文分类名比对全部失败，像是接口返回了错数据）。
        # 显式声明 charset 成本为零，却能消除一整类"乱码"故障。
        default_response_class=U8JSONResponse,
    )
    app.state.ctx = ctx

    _install_guard(app)
    _install_error_handlers(app)
    # 领域异常 → HTTP 的统一映射（服务层只抛 DomainError，不认识 HTTP）
    register_domain_error_handler(app)

    # ---- 探活 ---------------------------------------------------------------
    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        """最小探活响应：启动自检与外部脚本用它判断服务是否就绪。"""
        return {"status": "ok", "app": APP_ID, "version": __version__, "phase": BUILD_PHASE}

    # ---- 业务路由 -----------------------------------------------------------
    app.include_router(system_routes.router)
    app.include_router(meta_routes.router)
    app.include_router(accounts_routes.router)
    app.include_router(categories_routes.router)
    app.include_router(transactions_routes.router)
    app.include_router(taxonomy_routes.router)
    app.include_router(stats_routes.router)
    app.include_router(calendar_routes.router)
    app.include_router(assets_routes.router)
    app.include_router(planning_routes.router)
    app.include_router(templates_routes.router)
    app.include_router(trash_routes.router)
    app.include_router(kline_routes.router)

    # ---- 前端托管（必须最后注册，因为它是通配路由） -------------------------
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str, request: Request) -> Response:
        return _serve_static_or_index(request, full_path)

    return app


# -----------------------------------------------------------------------------
# 访问守卫
# -----------------------------------------------------------------------------
def _install_guard(app: FastAPI) -> None:
    """挂载访问守卫中间件（Host → Origin → 令牌）。"""

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        ctx = context_of(request.app)

        # ---- 关卡 1：Host 必须是环回地址 ------------------------------------
        if not is_loopback_host(request.headers.get("host")):
            _logger.warning("拒绝非环回 Host 请求：%r", request.headers.get("host"))
            return U8JSONResponse(
                {"detail": "仅允许通过本机环回地址访问"},
                status_code=403,
            )

        # ---- 关卡 2：Origin / Referer 白名单 --------------------------------
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin_allowed(origin, ctx.origin_whitelist):
            _logger.warning("拒绝非白名单来源请求：%r", origin)
            return U8JSONResponse({"detail": "请求来源不被允许"}, status_code=403)

        # ---- 关卡 3：令牌 ---------------------------------------------------
        path = request.url.path
        token_from_query = False
        if path not in PUBLIC_PATHS and _requires_token(path):
            candidate = _extract_token(request)
            token_from_query = candidate == request.query_params.get("token") and bool(candidate)
            if not ctx.token.matches(candidate):
                return _unauthorized(path)

        response: Response = await call_next(request)

        # 首次导航（URL 带 ?token=）成功后立即种下 Cookie：
        # 之后页面内的 fetch 与静态资源请求都会自动携带，前端无需管理令牌。
        if token_from_query:
            response.set_cookie(
                key=COOKIE_NAME,
                value=ctx.token.value,
                httponly=True,
                samesite="strict",
                path="/",
                # 注意：本地 http 不能设 Secure，否则 Cookie 会被浏览器丢弃
            )
        # 本地应用的响应一律禁止被缓存到磁盘（账本数据不落地到浏览器缓存）
        response.headers.setdefault("Cache-Control", "no-store")
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def _requires_token(path: str) -> bool:
    """判断路径是否需要令牌（规则见模块 docstring）。"""
    if path.startswith("/api/"):
        return True
    if path in {"", "/", "/index.html"}:
        return True
    # SPA 深链：无扩展名 → 返回 index.html → 需要令牌
    return Path(path).suffix == ""


def _extract_token(request: Request) -> str | None:
    """按优先级从 请求头 → Cookie → 查询参数 提取令牌。"""
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie

    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    return None


def _unauthorized(path: str) -> Response:
    """未授权响应。

    * ``/api/**`` → 结构化 JSON，便于前端统一处理；
    * 页面导航   → 一段人类可读的说明，避免用户看到裸 JSON 而困惑
      （正常使用不会走到这里，因为桌面外壳总会带上令牌）。
    """
    if path.startswith("/api/"):
        return U8JSONResponse({"detail": "会话令牌无效或已过期，请重新启动应用"}, status_code=401)
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{APP_NAME} · 需要从应用启动</title>
<style>:root{{color-scheme:light dark}}body{{margin:0;min-height:100vh;display:flex;
align-items:center;justify-content:center;font:15px/1.7 -apple-system,"Segoe UI Variable",
"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f7;color:#1d1d1f}}
@media (prefers-color-scheme:dark){{body{{background:#1c1c1e;color:#f5f5f7}}}}
.c{{max-width:560px;padding:32px 36px;border-radius:20px;background:rgba(255,255,255,.72);
box-shadow:0 12px 40px rgba(0,0,0,.12)}}h1{{font-size:18px;margin:0 0 10px;font-weight:600}}
p{{margin:8px 0;color:#6e6e73}}</style></head><body><div class="c">
<h1>需要通过 {APP_NAME} 应用打开</h1>
<p>本页面由本地记账服务提供，访问需要一次性会话令牌。</p>
<p>请从桌面快捷方式或开始菜单启动 {APP_NAME_EN}；若程序已在运行，请切换到已打开的窗口。</p>
</div></body></html>"""
    return HTMLResponse(html, status_code=401)


# -----------------------------------------------------------------------------
# 错误处理
# -----------------------------------------------------------------------------
def _install_error_handlers(app: FastAPI) -> None:
    """统一异常出口：日志留全栈，响应只给一句人话。

    这样做既避免把内部路径/堆栈泄露给调用方，又保证排查时日志里有完整证据。
    """

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:  # pragma: no cover
        _logger.exception("接口未处理异常：%s %s", request.method, request.url.path)
        return U8JSONResponse(
            {"detail": "服务内部错误，详情已写入日志", "error": type(exc).__name__},
            status_code=500,
        )


# -----------------------------------------------------------------------------
# 静态资源
# -----------------------------------------------------------------------------
def _serve_static_or_index(request: Request, full_path: str) -> Response:
    """优先返回真实文件；否则回落到 index.html（SPA 前端路由）。

    **路径穿越防护**：把候选路径 ``resolve()`` 后确认仍位于 ``web_dist`` 之内。
    仅靠字符串替换 ``..`` 是不够的（Windows 上还要考虑 ``\\``、短名、UNC 等），
    必须用解析后的绝对路径做前缀判断。
    """
    ctx = context_of(request.app)
    web_root = ctx.paths.web_dist

    if not ctx.paths.web_index.exists():
        return HTMLResponse(
            _FALLBACK_HTML.replace("%VERSION%", __version__).replace("%PHASE%", BUILD_PHASE),
            status_code=200,
        )

    candidate: Path | None = None
    if full_path:
        try:
            resolved = (web_root / full_path).resolve()
        except (OSError, RuntimeError):
            resolved = None
        if resolved is not None and _is_inside(resolved, web_root) and resolved.is_file():
            candidate = resolved

    if candidate is None:
        return _render_index(ctx)

    return _file_response(candidate)


def _is_inside(path: Path, root: Path) -> bool:
    """确认 ``path`` 位于 ``root`` 之内（双方均须为已解析的绝对路径）。"""
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _file_response(path: Path) -> Response:
    """返回静态文件，并补上正确的媒体类型与缓存策略。

    带内容哈希的 Vite 产物（``assets/index-abc123.js``）可以长期强缓存；
    index.html 必须不缓存（否则升级后用户仍看到旧版）。
    """
    from fastapi.responses import FileResponse

    headers = {"X-Content-Type-Options": "nosniff"}
    if "assets" in path.parts:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        headers["Cache-Control"] = "no-store"
    return FileResponse(path, headers=headers)


def _render_index(ctx: AppContext) -> Response:
    """渲染 index.html，并把启动引导数据注入 ``window.__AB_BOOT__``。

    为什么用「服务端注入」而不是另开一个 ``/api/boot`` 接口：
        * 前端首帧就能拿到主题、语言、版本，避免"先白屏再取配置"的闪烁；
        * 令牌随页面一起到达，静态资源与首个 API 调用可以立即发出。
    """
    html = ctx.paths.web_index.read_text(encoding="utf-8")
    prefs = ctx.config.snapshot()
    boot: dict[str, Any] = {
        "appId": APP_ID,
        "appName": APP_NAME,
        "appNameEn": APP_NAME_EN,
        "version": __version__,
        "phase": BUILD_PHASE,
        "token": ctx.token.value,
        "port": ctx.port,
        "serverTime": datetime.now().astimezone().isoformat(),
        "paths": {
            "dataDir": str(ctx.paths.data),
            "logFile": str(ctx.paths.log_file),
            "database": str(ctx.paths.database),
        },
        "runtime": {
            "portable": ctx.paths.portable,
            "frozen": ctx.paths.frozen,
        },
        "preferences": prefs.model_dump(mode="json"),
    }
    payload = json.dumps(boot, ensure_ascii=False)
    # 关键：转义 "</"，否则 JSON 中若出现 "</script>" 会提前闭合脚本标签
    payload = payload.replace("</", "<\\/")
    script = f"<script>window.__AB_BOOT__ = {payload};</script>"
    # 正常情况插入到 </head> 之前；结构异常时前置注入，绝不因此白屏
    html = html.replace("</head>", f"    {script}\n  </head>", 1) if "</head>" in html else script + html
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
