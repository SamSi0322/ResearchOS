from fastapi import APIRouter

from .approvals import global_router as approvals_global_router
from .approvals import project_router as approvals_project_router
from .audit import router as audit_router
from .context_bundles import router as context_bundles_router
from .drafts import router as drafts_router
from .health import router as health_router
from .ideas import router as ideas_router
from .packages import router as packages_router
from .projects import router as projects_router
from .providers import router as providers_router
from .reviews import router as reviews_router
from .runs import router as runs_router
from .sessions import router as sessions_router
from .smoke import router as smoke_router
from .specs import router as specs_router


def make_api_router() -> APIRouter:
    api = APIRouter()
    api.include_router(health_router, tags=["health"])
    api.include_router(providers_router, prefix="/providers", tags=["providers"])
    api.include_router(projects_router, prefix="/projects", tags=["projects"])
    api.include_router(ideas_router, prefix="/projects/{project_id}/ideas", tags=["ideas"])
    api.include_router(specs_router, prefix="/projects/{project_id}/specs", tags=["specs"])
    api.include_router(runs_router, prefix="/projects/{project_id}/runs", tags=["runs"])
    api.include_router(drafts_router, prefix="/projects/{project_id}/drafts", tags=["drafts"])
    api.include_router(
        reviews_router, prefix="/projects/{project_id}/reviews", tags=["reviews"]
    )
    api.include_router(
        packages_router, prefix="/projects/{project_id}/packages", tags=["packages"]
    )
    api.include_router(
        sessions_router, prefix="/projects/{project_id}/sessions", tags=["sessions"]
    )
    api.include_router(
        approvals_project_router,
        prefix="/projects/{project_id}/approvals",
        tags=["approvals"],
    )
    api.include_router(
        context_bundles_router,
        prefix="/projects/{project_id}/context-bundles",
        tags=["context-bundles"],
    )
    api.include_router(audit_router, prefix="/audit", tags=["audit"])
    api.include_router(smoke_router, prefix="/smoke", tags=["smoke"])
    api.include_router(
        approvals_global_router, prefix="/approvals", tags=["approvals"]
    )
    return api


__all__ = ["make_api_router"]
