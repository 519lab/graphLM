"""The LLM request body is the project's source: it must only go where the
user pointed it, and cleartext transport off-machine is called out."""

from __future__ import annotations

import logging

import pytest

from graphlm import llm
from graphlm.llm import GraphLLErrorResponse, call_llm


def _call(base_url: str = "https://api.example.test/v1") -> object:
    return call_llm(
        base_url=base_url,
        api_key="test-key",
        model="m",
        system_prompt="s",
        user_prompt="SOURCE_CODE_MARKER",
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_not_followed(httpx_mock, status):
    httpx_mock.add_response(
        url="https://api.example.test/v1/chat/completions",
        status_code=status,
        headers={"location": "https://elsewhere.example.test/collect"},
    )
    with pytest.raises(GraphLLErrorResponse) as exc:
        _call()
    msg = str(exc.value)
    assert "redirected" in msg and "elsewhere.example.test" in msg
    # Exactly one request, to the configured endpoint: the body never left
    # for the redirect target (pytest-httpx would also fail an unmatched one).
    sent = httpx_mock.get_requests()
    assert [str(r.url) for r in sent] == ["https://api.example.test/v1/chat/completions"]


@pytest.fixture(autouse=True)
def _reset_warned():
    llm._CLEARTEXT_WARNED.clear()
    yield
    llm._CLEARTEXT_WARNED.clear()


@pytest.mark.parametrize(
    "url,warns",
    [
        ("http://llm.example.test/v1", True),
        ("http://192.168.1.20:8000/v1", True),
        ("https://llm.example.test/v1", False),
        ("http://localhost:11434/v1", False),
        ("http://127.0.0.1:8000/v1", False),
        ("http://[::1]:8000/v1", False),
    ],
)
def test_cleartext_off_machine_warns(caplog, url, warns):
    with caplog.at_level(logging.WARNING, logger="graphlm.llm"):
        llm._warn_if_cleartext(url)
    assert ("plain http" in caplog.text) is warns


def test_cleartext_warning_is_once_per_url(caplog):
    with caplog.at_level(logging.WARNING, logger="graphlm.llm"):
        llm._warn_if_cleartext("http://llm.example.test/v1")
        llm._warn_if_cleartext("http://llm.example.test/v1")
    assert caplog.text.count("plain http") == 1
