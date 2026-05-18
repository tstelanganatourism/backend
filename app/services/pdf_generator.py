import logging
import asyncio
import sys
from typing import Optional
from datetime import datetime, timezone
from playwright.async_api import async_playwright

from app.core.config import settings
from app.services.r2_storage import r2_service
from app.db.session import AsyncSessionLocal
from app.models.package import Package
from app.models.room import Room
from app.models.enums import DocumentGenerationStatus
from app.utils.cache import clear_cache_prefix

logger = logging.getLogger(__name__)

async def generate_pdf_from_url(url: str, output_path: str = None) -> bytes:
    """
    Spins up headless Chromium, navigates to the URL, and generates a PDF.
    Returns the PDF bytes.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        page = await browser.new_page()
        
        # Navigate to the Next.js hidden print route
        logger.info(f"Navigating to {url} for PDF generation")
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        # Give some time for maps/images to fully render
        await asyncio.sleep(2)
        
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}
        )
        
        await browser.close()
        return pdf_bytes

def sync_generate_pdf(url: str) -> bytes:
    """
    Synchronous wrapper to run generate_pdf_from_url in a separate thread
    with its own ProactorEventLoop on Windows.
    """
    # Create a new event loop for this thread
    if sys.platform == 'win32':
        # Force proactor loop for subprocess support on Windows
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
        
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(generate_pdf_from_url(url))
    finally:
        loop.close()

async def generate_package_brochure_task(ctx, package_id: int):
    """
    Background task to generate a package brochure and upload to R2.
    """
    async with AsyncSessionLocal() as db:
        package = await db.get(Package, package_id)
        if not package:
            logger.error(f"Package {package_id} not found for brochure generation.")
            return
            
        try:
            # 1. Update status to GENERATING
            package.brochure_generation_status = DocumentGenerationStatus.GENERATING
            await db.commit()
            
            # 2. Generate PDF using a separate thread with its own ProactorEventLoop on Windows
            frontend_url = settings.FRONTEND_URL.rstrip('/')
            print_url = f"{frontend_url}/print/package/{package.slug}"
            pdf_bytes = await asyncio.to_thread(sync_generate_pdf, print_url)
            
            # 3. Upload to R2
            version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            object_name = f"private/brochures/generated/package_{package.slug}_{version}.pdf"
            await r2_service.upload_file(pdf_bytes, object_name, content_type="application/pdf")
            
            # 4. Clean up old generated brochure if exists and different
            if package.generated_brochure_url and package.generated_brochure_url != object_name:
                await r2_service.delete_file(package.generated_brochure_url)
                
            # 5. Update DB
            package.generated_brochure_url = object_name
            package.brochure_generation_status = DocumentGenerationStatus.AVAILABLE
            await db.commit()
            clear_cache_prefix("packages:list:")
            clear_cache_prefix(f"packages:detail:{package.slug}")
            logger.info(f"Successfully generated and uploaded brochure for package {package.slug}")
            
        except Exception as e:
            logger.error(f"Failed to generate brochure for package {package_id}: {e}")
            package.brochure_generation_status = DocumentGenerationStatus.FAILED
            await db.commit()
            raise e # Raise to let ARQ handle retries if applicable
