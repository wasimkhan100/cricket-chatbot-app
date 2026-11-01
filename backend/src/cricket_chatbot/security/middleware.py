"""HTTP middleware: request ids, security headers, body size limit, request metrics and access logs."""

import logging
import secrets
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..observability.logging import request_id_var
from ..observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("http")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_body_bytes: int):
        super().__init__(app)
        self.max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = secrets.token_hex(8)
        token = request_id_var.set(rid)
        t0 = time.monotonic()
        route = request.url.path if request.url.path.startswith(("/api", "/health", "/metrics")) else "/static"
        try:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_body_bytes:
                response: Response = JSONResponse({"detail": "request body too large"}, status_code=413)
            else:
                response = await call_next(request)
        except Exception:
            log.exception("unhandled error")
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        finally:
            request_id_var.reset(token)

        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api"):
            h.setdefault("Cache-Control", "no-store")

        elapsed = time.monotonic() - t0
        if not route.endswith("/stream") and "/chat" not in route:  # streaming routes are timed by the agent
            HTTP_LATENCY.labels(route).observe(elapsed)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        if route not in ("/metrics", "/static") and not route.startswith("/health"):
            log.info("request", extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                                       "ms": round(elapsed * 1000, 1), "request_id": rid})
        return response
