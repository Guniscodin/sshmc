from fastapi import APIRouter

from mcpanel.core import monitoring

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/stats")
def get_monitoring_stats():
    return monitoring.get_stats()


@router.get("/history")
def get_monitoring_history():
    return monitoring.get_history()
