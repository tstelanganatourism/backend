from fastapi import status

class AppError(Exception):
    """Base class for all application errors."""
    def __init__(self, message: str, status_code: int = status.HTTP_400_BAD_REQUEST, error_code: str = "BAD_REQUEST"):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(self.message)

class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status.HTTP_404_NOT_FOUND, "NOT_FOUND")

class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized"):
        super().__init__(message, status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")

class ForbiddenError(AppError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(message, status.HTTP_403_FORBIDDEN, "FORBIDDEN")

class InventoryError(AppError):
    def __init__(self, message: str = "Requested inventory is not available"):
        super().__init__(message, status.HTTP_409_CONFLICT, "INVENTORY_UNAVAILABLE")
