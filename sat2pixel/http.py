"""TLS-aware HTTP fetch.

Uses certifi's CA bundle by default. Some providers (e.g. AMap) serve an
incomplete certificate chain whose intermediate is not bundled by certifi, so
we fall back to an unverified context on certificate-verification errors only.
"""

import ssl
import urllib.error
import urllib.request

import certifi

_VERIFIED = ssl.create_default_context(cafile=certifi.where())
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


def urlopen(url, timeout=15, headers=None):
    hdrs = {"User-Agent": "RaceBrick_Sat2Pixel/0.2"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_VERIFIED)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            return urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED)
        raise
