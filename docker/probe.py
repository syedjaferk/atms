"""Health check for the containers. Asks one URL and exits 0 when it answers.

Goes straight to the address and ignores any HTTP proxy settings in the environment, because a proxy set for the whole machine
(common on office and college networks) would otherwise send this local check to the proxy and mark a healthy part unhealthy.
"""
import sys
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.open(sys.argv[1], timeout=4)
except Exception as e:                               # noqa: BLE001
    print(f"not ready, {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
