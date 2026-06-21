"""
Shared HTTP infrastructure — SSL bypass adapter and session factory.
"""

import ssl
import requests
from requests.adapters import HTTPAdapter


class SSLBypassAdapter(HTTPAdapter):
    """
    Custom Transport Adapter that completely bypasses SSL certificate verification.
    Needed for corporate proxies / firewalls that intercept HTTPS with their own certs.
    """
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        proxy_kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _make_ssl_session() -> requests.Session:
    """Create a requests.Session with SSL bypass mounted."""
    s = requests.Session()
    adapter = SSLBypassAdapter()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.verify = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    return s