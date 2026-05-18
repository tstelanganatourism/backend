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

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "details": exc.errors()}}
        )
        
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # Extremely brutal catch-all for unhandled 500s. Must log the traceback.
        # Use loguru in real implementation.
        print(f"Unhandled Exception: {str(exc)}")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred."}}
        )
