from fastapi import APIRouter

router = APIRouter(tags=["Health"])


# Health endpoint used for service monitoring and readiness checks.
@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "api",
    }