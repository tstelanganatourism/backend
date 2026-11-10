import time
import uuid
from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send, Message


class RequestIDMiddleware:
    """
    Pure ASGI Request-ID + timing observability middleware.
    Avoids Starlette BaseHTTPMiddleware so large multipart uploads and SSE streams
    pass straight through without buffering request/response bodies in memory.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                request_id = value.decode("latin-1")
                break
        if not request_id:
            request_id = uuid.uuid4().hex

        scope.setdefault("state", {})["request_id"] = request_id

        start_time = time.perf_counter()
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        client = scope.get("client")
        client_host = client[0] if client else "unknown"
        is_asset = path.startswith(("/static", "/_next")) or path.endswith(
            (".js", ".css", ".png", ".jpg", ".ico", ".svg")
        )
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with logger.contextualize(request_id=request_id):
            if not is_asset:
                logger.info("Incoming request: {} {} from {}", method, path, client_host)

            try:
                await self.app(scope, receive, send_wrapper)
                duration = time.perf_counter() - start_time
                if not is_asset:
                    logger.info(
                        "Completed request: {} {} - Status: {} in {:.2f}ms",
                        method,
                        path,
                        status_code,
                        duration * 1000,
                    )
            except Exception as e:
                duration = time.perf_counter() - start_time
                logger.error(
                    "Failed request: {} {} - Error: {} in {:.2f}ms",
                    method,
                    path,
                    str(e),
                    duration * 1000,
                    exc_info=True,
                )
                raise
