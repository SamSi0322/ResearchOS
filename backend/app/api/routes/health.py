from fastapi import APIRouter

from app import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "researchos", "version": __version__}
