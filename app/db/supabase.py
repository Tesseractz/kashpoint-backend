import os
import threading
import time
from typing import Callable, TypeVar

from dotenv import load_dotenv
from supabase import Client, create_client

T = TypeVar("T")

# One client per thread, not one per process. The sync client multiplexes
# every request over a single HTTP/2 connection, and Starlette runs sync
# endpoints on a thread pool, so a process-wide client had several threads
# reading the same socket whenever a fresh account's first page load arrived
# as a burst. That surfaced as "[Errno 11] Resource temporarily unavailable"
# and a failed request. Thread-local clients never share a connection.
_local = threading.local()


def get_supabase_client() -> Client:
    """Returns this thread's Supabase client, configured from environment variables."""
    client = getattr(_local, "client", None)
    if client is not None:
        return client

    load_dotenv()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables."
        )

    client = create_client(supabase_url, supabase_key)
    _local.client = client
    return client


def reset_supabase_client() -> None:
    """Forget this thread's client so the next call builds a fresh one (tests)."""
    _local.client = None


# Transient transport failures: a dropped or reset connection, a slow answer,
# the socket error above. Each deserves one short retry, not a failed request.
_TRANSIENT_MARKERS = (
    "errno 11",
    "resource temporarily unavailable",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "server disconnected",
    "remote protocol error",
    "readerror",
    "writeerror",
    "connecterror",
)
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.2


def is_transient_error(err: BaseException) -> bool:
    text = f"{type(err).__name__} {err}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def retry_transient(op: Callable[[], T]) -> T:
    """Run op(), retrying transient transport errors with a short backoff.

    Anything that is not transient is raised at once; a transient error on the
    last attempt is raised too, so callers still see a real failure.
    """
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return op()
        except Exception as e:
            if not is_transient_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    raise AssertionError("unreachable")
