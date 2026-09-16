"""Shared HTTP plumbing for the daily data scripts.

Every upstream fetch in scripts/ goes through here so that:
  * a transient empty/failed response is retried once instead of writing off the run;
  * any failure surfaces as ONE actionable line (which source, what came back)
    instead of a stack trace, so a red daily run can be diagnosed from the log alone.
"""
import json
import ssl
import sys
import time
import urllib.error
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


def _short(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


def fetch_text(url: str, data: bytes = None, attempts: int = ATTEMPTS) -> str:
    """Fetch url (POST when data is given) and return the body.

    Raises DataSourceError - never a bare URLError/timeout - so each caller can
    decide whether that particular source failing is fatal for its page. Pass
    attempts=1 for a source that already degrades on its own: retrying a feed
    whose failure is harmless only makes a bad day slower.
    """
    problem = "unknown error"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_relaxed_ssl_context()) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            problem = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break  # a 4xx is a permanent answer; retrying only wastes runner time
        except OSError as e:  # URLError, timeouts, resets - all OSError subclasses
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
        # These endpoints answer 200 with an HTML error/maintenance page often
        # enough that it has to be a describable failure, not a JSONDecodeError
        # traceback from somewhere deep in the call stack.
        raise DataSourceError(f"{url}: expected JSON, got {_short(body)}") from None


def step(label: str, fn, *args, **kwargs):
    """Run one stage of a daily job.

    Any failure ends the run with a single 'ERROR: <label>: <cause>' line, which
    is all of the log anyone needs to read to know which source or parser broke.
    """
    try:
        return fn(*args, **kwargs)
    except DataSourceError as e:
        sys.exit(f"ERROR: {label}: {e}")
    except (RuntimeError, ValueError, KeyError, IndexError, AttributeError, TypeError) as e:
        sys.exit(f"ERROR: {label}: {type(e).__name__}: {e}")
