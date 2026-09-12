from aiogram import Router

from app.bot.routers import (
    admin,
    circle,
    fallback,
    metadata,
    optimizer,
    start,
    stickers,
    voice,
    watermark,
)


def build_root_router() -> Router:
    """Assemble the router tree.

    Routers are module-level singletons (the aiogram convention), so this may
    only be called once per process. Order matters: the fallback router, which
    matches any message, must stay last.
    """
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(circle.router)
    root.include_router(voice.router)
    root.include_router(optimizer.router)
    root.include_router(watermark.router)
    root.include_router(metadata.router)
    root.include_router(stickers.router)
    root.include_router(admin.router)
    root.include_router(fallback.router)
    return root


__all__ = ["build_root_router"]
