"""Shared fetching for the daily data scripts.

Every upstream request goes through here so a transient empty response is
retried once, and so any failure reads as one actionable line - which source,
what came back - instead of a traceback nobody can act on from the job log.
"""
import json
import ssl
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TIMEOUT = 30
ATTEMPTS = 2
RETRY_WAIT = 3.0


class DataSourceError(RuntimeError):
    """An upstream report was unreachable, empty, or not in the format we asked for."""


def _relaxed_ssl_context() -> ssl.SSLContext:
    # Some TWSE/TPEx cert chains trip urllib's default strict x509 checks
    # (Missing Subject Key Identifier) even though browsers/curl accept them.
    # Only relax that one check; keep the rest of certificate verification intact.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def fetch_text(url: str, data: bytes = None, attempts: int = ATTEMPTS) -> str:
    """Fetch url (POST when data is given). Raises DataSourceError, never a bare
    URLError/timeout, so each caller decides whether that source is fatal for its
    page. attempts=1 for a source that already degrades on its own."""
    problem = "unknown error"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_relaxed_ssl_context()) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        except OSError as e:  # URLError, HTTPError and timeouts are all OSError
            problem = f"{type(e).__name__}: {e}"
        else:
            if body.strip():
                return body
            problem = "empty response body"
        if attempt < attempts:
            time.sleep(RETRY_WAIT)
    raise DataSourceError(f"{url}: {problem}")


def fetch_json(url: str, data: bytes = None, attempts: int = ATTEMPTS):
    body = fetch_text(url, data, attempts)
    try:
        return json.loads(body)
    except ValueError:
        # These endpoints answer 200 with an HTML error page often enough that it
        # has to be a describable failure, not a JSONDecodeError from deep in the stack.
        head = " ".join(body.split())[:120]
        raise DataSourceError(f"{url}: expected JSON, got {head}") from None


def step(label: str, fn, *args, **kwargs):
    """Run one stage of a daily job. Any failure ends the run with a single
    'ERROR: <label>: <cause>' line - all the log a fix needs."""
    try:
        return fn(*args, **kwargs)
    except DataSourceError as e:
        sys.exit(f"ERROR: {label}: {e}")
    except Exception as e:
        sys.exit(f"ERROR: {label}: {type(e).__name__}: {e}")
