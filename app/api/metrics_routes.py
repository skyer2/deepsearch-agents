"""
【Phase 9】Metrics API — 在线可观测性聚合

GET /api/metrics/summary   JSON 仪表盘（默认 7 天窗口）
GET /api/metrics/prometheus Prometheus text exposition
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from app.api.observability_metrics import aggregate_metrics, render_prometheus_text
from app.config.loader import get_harness_config

ROOT = Path(__file__).resolve().parents[1]

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _log_dir() -> Path:
    config = get_harness_config()
    return ROOT / config.jsonl_log_dir


@router.get("/summary")
def metrics_summary(
    window_hours: int = Query(default=0, ge=0, le=720),
) -> dict:
    """滚动窗口 Harness 运行指标（来自 JSONL run_summary）。"""
    config = get_harness_config()
    hours = window_hours or config.metrics_window_hours
    metrics = aggregate_metrics(_log_dir(), window_hours=hours)
    payload = metrics.to_dict()
    payload["enabled"] = config.metrics_enabled
    payload["source"] = "jsonl_run_summary"
    return payload


@router.get("/prometheus")
def metrics_prometheus(
    window_hours: int = Query(default=0, ge=0, le=720),
) -> PlainTextResponse:
    """Prometheus scrape 端点（Grafana / Datadog 等可对接）。"""
    config = get_harness_config()
    if not config.prometheus_enabled:
        return PlainTextResponse(
            "# harness metrics disabled\n",
            media_type="text/plain; version=0.0.4",
        )
    hours = window_hours or config.metrics_window_hours
    metrics = aggregate_metrics(_log_dir(), window_hours=hours)
    return PlainTextResponse(
        render_prometheus_text(metrics),
        media_type="text/plain; version=0.0.4",
    )
