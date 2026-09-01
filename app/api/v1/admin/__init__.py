from fastapi import APIRouter
from app.api.v1.admin import dashboard, settings, packages, rooms, media, inventory, coupons, agents, bookings, users, pre_bookings as admin_pre_bookings

router = APIRouter()

router.include_router(dashboard.router)
router.include_router(settings.router)
router.include_router(packages.router)
router.include_router(rooms.router)
router.include_router(media.router)
router.include_router(inventory.router)
router.include_router(coupons.router)
router.include_router(agents.router)
router.include_router(bookings.router)
router.include_router(users.router)
router.include_router(admin_pre_bookings.router)

