import json
import time

import requests

BASE_URL = "https://rt.data.gov.hk/v2/transport/citybus"
RETRIES = 4
TIMEOUT = (5, 25)

_session = requests.Session()
_session.headers.update({
    "User-Agent": "eta6-ctb/2.0 (+https://github.com/336699c/eta6)",
    "Accept": "application/json",
})


def get_json(url):
    """GET a JSON document with retries and a hard timeout.

    Raises RuntimeError when every attempt fails, so callers can decide
    whether a missing record is fatal instead of silently writing nulls.
    """
    last_error = None
    for attempt in range(RETRIES):
        try:
            response = _session.get(url, timeout=TIMEOUT)
            if response.status_code == 200:
                return response.json()
            last_error = "HTTP %s" % response.status_code
            if response.status_code in (400, 404):
                break
            if response.status_code in (403, 429):
                # Rate limited by the data.gov.hk gateway: back off hard.
                time.sleep(15 * (attempt + 1))
                continue
        except (requests.RequestException, ValueError) as exc:
            last_error = repr(exc)
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("GET %s failed: %s" % (url, last_error))


def get_req(link, data=""):
    """Backwards-compatible raw byte fetch."""
    last_error = None
    for attempt in range(RETRIES):
        try:
            response = _session.get(link, timeout=TIMEOUT)
            if response.status_code == 200:
                if data:
                    return response.content, data
                return response.content
            last_error = "HTTP %s" % response.status_code
        except requests.RequestException as exc:
            last_error = repr(exc)
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("GET %s failed: %s" % (link, last_error))
