from fastapi import FastAPI

from services.api.core.exceptions import (
    global_exception_handler,
)
from services.api.core.logging import (
    configure_logging,
)
from services.api.middleware.logging import (
    logging_middleware,
)
from services.api.middleware.request_id import (
    request_id_middleware,
)
from services.api.routes.health import (
    router as health_router,
)
from services.api.core.config import (
    settings
)

# ============================================================================
# Application Bootstrap
# ============================================================================

configure_logging()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)

# ============================================================================
# Middleware Registration
# ============================================================================

app.middleware("http")(request_id_middleware)
app.middleware("http")(logging_middleware)

# ============================================================================
# Global Exception Handlers
# ============================================================================

app.add_exception_handler(
    Exception,
    global_exception_handler,
)

app.include_router(health_router)

# How future API to be places:
# routes/
# ├── health.py
# ├── users.py
# ├── analytics.py
# ├── reports.py
# ├── cameras.py
# └── alerts.py


# Addition of APIs into the main.py file.
# app.include_router(users_router, prefix="/users")
# app.include_router(reports_router, prefix="/reports")
# app.include_router(cameras_router, prefix="/cameras")