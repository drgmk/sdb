from __future__ import annotations

import threading
import webbrowser
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from .service import IdentityService


def create_review_app(
    session_factory: sessionmaker[Session], *, sample: str | None = None,
    identity_service_factory: Callable[[], IdentityService] | None = None,
    catalog_service_factory: Callable[[str, str], object] | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_factory: Callable[[], object] | None = None,
    reference_store: object | None = None,
):
    try:
        from fastapi import FastAPI
    except ImportError as error:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "review UI dependencies are missing; install with pip install -e '.[review]'"
        ) from error

    from .review_routes_decisions import register_decision_routes
    from .review_routes_imports import register_import_routes
    from .review_routes_pages import register_page_routes
    from .review_web_context import ReviewWebContext

    app = FastAPI(title="SDB review", docs_url=None, redoc_url=None)
    context = ReviewWebContext(
        session_factory=session_factory,
        sample=sample,
        identity_service_factory=identity_service_factory,
        catalog_service_factory=catalog_service_factory,
        catalog_coverage_providers=catalog_coverage_providers,
        catalog_update_factory=catalog_update_factory,
        reference_store=reference_store,
    )
    register_page_routes(app, context)
    register_decision_routes(app, context)
    register_import_routes(app, context)
    return app


def serve_review_ui(
    session_factory: sessionmaker[Session],
    *,
    sample: str | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    identity_service_factory: Callable[[], IdentityService] | None = None,
    catalog_service_factory: Callable[[str, str], object] | None = None,
    catalog_coverage_providers: tuple[str, ...] | None = None,
    catalog_update_factory: Callable[[], object] | None = None,
    reference_store: object | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the review UI currently binds to localhost only")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "review UI dependencies are missing; install with pip install -e '.[review]'"
        ) from error
    app = create_review_app(
        session_factory,
        sample=sample,
        identity_service_factory=identity_service_factory,
        catalog_service_factory=catalog_service_factory,
        catalog_coverage_providers=catalog_coverage_providers,
        catalog_update_factory=catalog_update_factory,
        reference_store=reference_store,
    )
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
