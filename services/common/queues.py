"""Queue names and Redis keys shared by the API and workers."""

from arq.constants import health_check_key_suffix

SCRAPE_QUEUE = "he:scrape"
LLM_QUEUE = "he:llm"
NOTIFY_QUEUE = "he:notify"
SCHEDULER_QUEUE = "he:scheduler"
ALL_QUEUES = (SCRAPE_QUEUE, LLM_QUEUE, NOTIFY_QUEUE, SCHEDULER_QUEUE)

SCHEDULER_HEARTBEAT_KEY = "he:scheduler:heartbeat"


def health_key(queue: str) -> str:
    """Key each ARQ worker refreshes while alive."""
    return queue + health_check_key_suffix
