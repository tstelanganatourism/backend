import time
import uuid
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger

# Configure standard logging to redirect to loguru if needed, or rely directly on loguru
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Extract or generate unique Request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = uuid.uuid4().hex

        # Store Request ID on request state for use in application endpoints if needed
        request.state.request_id = request_id

        # 2. Measure execution time and log contextual request data
        start_time = time.perf_counter()
        
        # Exempt asset routes or health check logs from heavy logging if desired
        path = request.url.path
        is_asset = path.startswith(("/static", "/_next")) or path.endswith((".js", ".css", ".png", ".jpg", ".ico", ".svg"))
        
        # Bind the request ID to all loguru logs triggered during this async task context
        with logger.contextualize(request_id=request_id):
            if not is_asset:
                logger.info(f"Incoming request: {request.method} {path} from {request.client.host if request.client else 'unknown'}")

            try:
                response = await call_next(request)
                duration = time.perf_counter() - start_time
                
                if not is_asset:
                    logger.info(f"Completed request: {request.method} {path} - Status: {response.status_code} in {duration*1000:.2f}ms")
                
                # Propagate Request ID back to the client in response headers
                response.headers["X-Request-ID"] = request_id
                return response
            except Exception as e:
                duration = time.perf_counter() - start_time
                logger.error(f"Failed request: {request.method} {path} - Error: {str(e)} in {duration*1000:.2f}ms", exc_info=True)
                raise e
