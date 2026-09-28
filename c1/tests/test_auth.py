"""API key authorization. No AWS calls."""

from __future__ import annotations

import pytest

from c1.api import auth

KEY = "6f1c2b9d4e8a70135c9f2a4b6d8e0f1234567890abcdef1234567890abcdef12"
ARN = "arn:aws:ssm:eu-west-2:123456789012:parameter/guardrails-demo/api-key"


class FakeSsm:
    """Records GetParameter calls and returns a canned value."""

    def __init__(self, value: str = KEY, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[dict] = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Name": kwargs["Name"], "Value": self.value}}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv(auth.PARAMETER_ARN_VARIABLE, ARN)
    auth.reset_cache()
    yield
    auth.reset_cache()


# --- extract_presented_key -------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": KEY},
        {"Authorization": KEY},
        {"AUTHORIZATION": KEY},
        {"authorization": f"Bearer {KEY}"},
        {"authorization": f"bearer {KEY}"},
        {"authorization": f"  Bearer   {KEY}  "},
        {"content-type": "application/json", "authorization": KEY},
    ],
)
def test_extract_presented_key_accepts_bare_and_bearer(headers):
    assert auth.extract_presented_key(headers) == KEY


@pytest.mark.parametrize(
    "headers",
    [None, {}, {"authorization": ""}, {"authorization": "   "}, {"authorization": "Bearer "}],
)
def test_extract_presented_key_returns_none_when_absent(headers):
    assert auth.extract_presented_key(headers) is None


def test_extract_presented_key_ignores_other_headers():
    assert auth.extract_presented_key({"x-api-key": KEY}) is None


# --- expected_key ----------------------------------------------------------


def test_expected_key_reads_the_parameter_by_arn():
    ssm = FakeSsm()
    assert auth.expected_key(client=ssm) == KEY
    assert ssm.calls == [{"Name": ARN, "WithDecryption": True}]


def test_expected_key_caches_within_the_ttl():
    ssm = FakeSsm()
    auth.expected_key(client=ssm, now=1000.0)
    auth.expected_key(client=ssm, now=1000.0 + auth.CACHE_TTL_SECONDS - 1)
    assert len(ssm.calls) == 1


def test_expected_key_refetches_after_the_ttl():
    ssm = FakeSsm()
    auth.expected_key(client=ssm, now=1000.0)
    ssm.value = "rotated-key"
    assert (
        auth.expected_key(client=ssm, now=1000.0 + auth.CACHE_TTL_SECONDS + 1)
        == "rotated-key"
    )
    assert len(ssm.calls) == 2


def test_expected_key_raises_when_the_arn_is_missing(monkeypatch):
    monkeypatch.delenv(auth.PARAMETER_ARN_VARIABLE, raising=False)
    with pytest.raises(auth.AuthNotConfigured):
        auth.expected_key(client=FakeSsm())


def test_expected_key_raises_when_the_arn_is_blank(monkeypatch):
    monkeypatch.setenv(auth.PARAMETER_ARN_VARIABLE, "   ")
    with pytest.raises(auth.AuthNotConfigured):
        auth.expected_key(client=FakeSsm())


def test_expected_key_raises_auth_unavailable_when_ssm_fails():
    ssm = FakeSsm(error=RuntimeError("AccessDeniedException"))
    with pytest.raises(auth.AuthUnavailable):
        auth.expected_key(client=ssm)


def test_expected_key_raises_auth_unavailable_on_an_empty_parameter():
    with pytest.raises(auth.AuthUnavailable):
        auth.expected_key(client=FakeSsm(value=""))


def test_a_failed_fetch_is_not_cached():
    ssm = FakeSsm(error=RuntimeError("throttled"))
    with pytest.raises(auth.AuthUnavailable):
        auth.expected_key(client=ssm)
    ssm.error = None
    assert auth.expected_key(client=ssm) == KEY


# --- is_authorized ---------------------------------------------------------


def test_is_authorized_accepts_the_matching_key():
    assert auth.is_authorized({"authorization": KEY}, client=FakeSsm()) is True


def test_is_authorized_accepts_the_bearer_form():
    assert auth.is_authorized({"authorization": f"Bearer {KEY}"}, client=FakeSsm()) is True


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"authorization": "wrong-key"},
        {"authorization": KEY[:-1]},
        {"authorization": KEY + "x"},
        {"authorization": KEY.upper()},
        {"x-api-key": KEY},
    ],
)
def test_is_authorized_rejects_anything_else(headers):
    assert auth.is_authorized(headers, client=FakeSsm()) is False


def test_is_authorized_propagates_a_configuration_failure(monkeypatch):
    """A broken check must not look like a rejected key."""
    monkeypatch.delenv(auth.PARAMETER_ARN_VARIABLE, raising=False)
    with pytest.raises(auth.AuthNotConfigured):
        auth.is_authorized({"authorization": KEY}, client=FakeSsm())


def test_is_authorized_propagates_an_unavailable_parameter():
    ssm = FakeSsm(error=RuntimeError("timeout"))
    with pytest.raises(auth.AuthUnavailable):
        auth.is_authorized({"authorization": KEY}, client=ssm)


def test_non_ascii_key_does_not_crash_the_comparison():
    assert auth.is_authorized({"authorization": "kéy-wîth-ünicode"}, client=FakeSsm()) is False
