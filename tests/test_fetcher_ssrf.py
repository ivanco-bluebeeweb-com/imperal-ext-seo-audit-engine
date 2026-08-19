"""Security (Part D3, SCENARIO_TESTING_STANDARD.md) -- SEO Audit Engine.

This app is fundamentally different from the rest of the portfolio: its
whole PURPOSE is to crawl arbitrary user-supplied domains over real HTTP
(`audit_sites` accepts any string a user types). That is a genuine SSRF
surface, not a false positive -- unlike e.g. Make.com's owner-configured
outgoing webhook, here the target is *whatever the caller names*.

A real gap was found during PST Part D: `seoaudit/fetcher.py` had NO check
on the resolved IP before connecting, on either the initial request or any
manual redirect hop. A user could point `audit_sites` at 127.0.0.1, an
internal service behind a firewall, or a cloud metadata endpoint
(169.254.169.254), and the fetcher would happily connect. This was fixed by
adding `_check_host_is_public()` (resolves the host, rejects any private/
loopback/link-local/reserved/multicast/unspecified address) called on every
hop of `Fetcher.fetch()`, including redirects.

Known residual risk (documented in fetcher.py's own module docstring): DNS
rebinding in the narrow window between the check's resolve and urllib's own
connect is NOT closed by this fix -- doing so would need replacing urllib's
transport to pin the already-checked IP, which is a bigger change than this
audit's scope. This is a documented trade-off, not an oversight.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seoaudit.fetcher import Fetcher, FetchPolicy, _check_host_is_public


class TestCheckHostIsPublic(unittest.TestCase):
    """Direct unit tests on the resolver-based guard, no network needed."""

    def test_blocks_loopback_ip_literal(self):
        self.assertIsNotNone(_check_host_is_public("127.0.0.1"))

    def test_blocks_loopback_hostname(self):
        self.assertIsNotNone(_check_host_is_public("localhost"))

    def test_blocks_cloud_metadata_endpoint(self):
        """169.254.169.254 -- AWS/GCP/Azure instance metadata. The single
        most damaging SSRF target there is: it hands over live cloud
        credentials to whoever can reach it."""
        self.assertIsNotNone(_check_host_is_public("169.254.169.254"))

    def test_blocks_private_class_a(self):
        self.assertIsNotNone(_check_host_is_public("10.0.0.5"))

    def test_blocks_private_class_c(self):
        self.assertIsNotNone(_check_host_is_public("192.168.1.1"))

    def test_blocks_ipv6_loopback(self):
        self.assertIsNotNone(_check_host_is_public("[::1]"))

    def test_blocks_ipv6_unique_local(self):
        self.assertIsNotNone(_check_host_is_public("[fd00::1]"))

    def test_allows_public_ip_literal(self):
        """8.8.8.8 (Google DNS) is a real public address -- must NOT be
        blocked, or the audit would refuse every legitimate site."""
        self.assertIsNone(_check_host_is_public("8.8.8.8"))

    def test_port_suffix_does_not_bypass_the_check(self):
        """A host of '127.0.0.1:8080' must still resolve the hostname part
        and get blocked -- the port must not smuggle a blocked IP past
        string-based filtering."""
        self.assertIsNotNone(_check_host_is_public("127.0.0.1:8080"))

    def test_unresolvable_host_is_rejected_not_silently_allowed(self):
        """A hostname that fails DNS resolution must be treated as blocked
        (fail-closed), never silently passed through as 'safe'."""
        self.assertIsNotNone(
            _check_host_is_public("this-domain-does-not-exist-imperal-test.invalid"))


class TestFetchRefusesBlockedHosts(unittest.TestCase):
    """End-to-end through Fetcher.fetch() -- confirms the guard is actually
    wired into the real request path, not just defined and unused."""

    def setUp(self):
        self.fetcher = Fetcher(FetchPolicy(timeout=2.0))

    def test_fetch_refuses_loopback_url_without_any_network_call(self):
        result = self.fetcher.fetch("http://127.0.0.1/admin")
        self.assertFalse(result["ok"])
        self.assertIn("заблокирован", result["error"])

    def test_fetch_refuses_cloud_metadata_url(self):
        result = self.fetcher.fetch("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(result["ok"])
        self.assertIn("заблокирован", result["error"])

    def test_fetch_refuses_non_http_scheme(self):
        """file:// or gopher:// URLs must never reach urllib.request at
        all -- SSRF via scheme confusion, not just host confusion."""
        result = self.fetcher.fetch("file:///etc/passwd")
        self.assertFalse(result["ok"])
        self.assertIn("схема", result["error"])

    def test_fetch_never_raises_on_a_blocked_host(self):
        """fetch()'s whole contract (per its own docstring) is to never
        raise -- a blocked host must return the same clean dict shape as
        any other failure, not throw SSRFBlockedError up to the caller."""
        try:
            result = self.fetcher.fetch("http://localhost/")
        except Exception as e:  # pragma: no cover -- this is the failure mode
            self.fail(f"fetch() raised {type(e).__name__} instead of returning ok=False: {e}")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
