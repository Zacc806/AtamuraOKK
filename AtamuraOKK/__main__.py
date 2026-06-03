import uvicorn

from AtamuraOKK.settings import settings


def main() -> None:
    """Entrypoint of the application."""
    uvicorn.run(
        "AtamuraOKK.web.application:get_app",
        workers=settings.workers_count,
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level.value.lower(),
        access_log=True,
        factory=True,
    )


if __name__ == "__main__":
    main()
