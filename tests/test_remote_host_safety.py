import os
from unittest import mock

import pytest

import caldav_client
import imap_search
import remote_host_safety


PUBLIC_RESULT = [(2, 1, 6, "", ("93.184.216.34", 443))]
PRIVATE_RESULT = [(2, 1, 6, "", ("127.0.0.1", 443))]


def test_public_host_is_allowed():
    with mock.patch("remote_host_safety.socket.getaddrinfo", return_value=PUBLIC_RESULT):
        assert remote_host_safety.validate_remote_host(
            "mail.example.com", 993, purpose="IMAP"
        ) == "mail.example.com"


def test_private_host_is_blocked_by_default():
    with mock.patch("remote_host_safety.socket.getaddrinfo", return_value=PRIVATE_RESULT):
        with pytest.raises(remote_host_safety.UnsafeRemoteHost, match="private or reserved"):
            remote_host_safety.validate_remote_host("mail.internal", 993, purpose="IMAP")


def test_private_host_requires_explicit_owner_opt_in():
    private_address = ".".join(("192", "168", "1", "20"))
    with mock.patch.dict(os.environ, {remote_host_safety.PRIVATE_HOSTS_ENV: "1"}):
        with mock.patch("remote_host_safety.socket.getaddrinfo") as resolve:
            assert remote_host_safety.validate_remote_host(
                private_address, 993, purpose="IMAP"
            ) == private_address
            resolve.assert_not_called()


def test_credentialed_url_requires_https_and_no_userinfo():
    with pytest.raises(remote_host_safety.UnsafeRemoteHost, match="https"):
        remote_host_safety.validate_https_url("http://example.com/calendar", purpose="DAV")
    with pytest.raises(remote_host_safety.UnsafeRemoteHost, match="must not contain credentials"):
        remote_host_safety.validate_https_url(
            "https://user:pass@example.com/calendar", purpose="DAV"
        )


def test_imap_rejects_private_target_before_connecting():
    with mock.patch("remote_host_safety.socket.getaddrinfo", return_value=PRIVATE_RESULT):
        with mock.patch("imap_search.imaplib.IMAP4_SSL") as connect:
            with pytest.raises(imap_search.EmailSearchError, match="private or reserved"):
                imap_search.email_search(
                    "owner@example.com", "password", imap_host="mail.internal"
                )
            connect.assert_not_called()


def test_dav_rejects_private_target_before_sending_credentials():
    with mock.patch("remote_host_safety.socket.getaddrinfo", return_value=PRIVATE_RESULT):
        with mock.patch("caldav_client.urllib.request.build_opener") as opener:
            with pytest.raises(caldav_client.CalDavError, match="private or reserved"):
                caldav_client._dav_request(
                    "REPORT", "https://calendar.internal/events", "owner", "secret"
                )
            opener.assert_not_called()
