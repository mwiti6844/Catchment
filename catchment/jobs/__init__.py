"""Async work. Plain RQ over Redis — no n8n, per CLAUDE.md."""

from catchment.jobs.pipeline import PipelineResult, process_item, run_pipeline
from catchment.jobs.queue import QUEUE_NAME, RQTaskQueue, get_pipeline_queue

__all__ = [
    "QUEUE_NAME",
    "PipelineResult",
    "RQTaskQueue",
    "get_pipeline_queue",
    "process_item",
    "run_pipeline",
]
