import sys
from loguru import logger
from app.core.config import settings

def setup_logging():
    # Remove default handler
    logger.remove()
    
    # Add structured JSON logging for production, or pretty logging for dev
    if settings.ENVIRONMENT == "production":
        logger.add(
            sys.stdout,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            serialize=True, # Structured JSON output
            level="INFO"
        )
    else:
        logger.add(
            sys.stdout,
            colorize=True,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level="DEBUG"
        )
