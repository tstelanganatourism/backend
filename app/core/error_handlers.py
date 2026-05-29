from fastapi import Request, FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from app.core.exceptions import AppError
import traceback

def setup_exception_handlers(app: FastAPI):
    
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        # In production, we'd log this structured with loguru
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.error_code, "message": exc.message}}
        )

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        def clean_value(val):
            if isinstance(val, dict):
                return {k: clean_value(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [clean_value(v) for v in val]
            elif isinstance(val, Exception):
                return str(val)
            elif not isinstance(val, (str, int, float, bool, type(None))):
                return str(val)
            return val

        try:
            errors = clean_value(exc.errors())
        except Exception:
            errors = str(exc)

        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "details": errors}}
        )
        
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).error(f"Unhandled Exception: {str(exc)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred."}}
        )
