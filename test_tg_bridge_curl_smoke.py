"""Smoke tests for tg_bridge curl-first fallback (Sprint 5.5 hardening).

We mock subprocess.run + urllib.request.urlopen to verify:
* `_post_via_curl` happy path returns the parsed JSON dict.
* `_post_via_curl` translates TG error_code=429 → TGRateLimitError.
* `_post_via_curl` translates error_code=401 → TGAuthError.
* `_post` falls back to urllib when curl fails with a generic RuntimeError.
* `_post` does NOT swallow TGAuthError (config errors should bubble up).

Run: .venv/bin/python test_tg_bridge_curl_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch, MagicMock

# Force a non-empty bot_token so _post doesn't bail with TGAuthError before
# the curl/urllib code paths run. We never actually call api.telegram.org.
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-for-mock-only"
os.environ["TG_CHAT_ID"] = "0"  # not used by _post itself

# Drop cached modules so config sees our env overrides.
for mod in ("config", "tg_bridge"):
    sys.modules.pop(mod, None)

import tg_bridge  # noqa: E402  (must come after env setup)


def _fake_completed_process(stdout: str, returncode: int = 0,
                            stderr: str = "") -> MagicMock:
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def test_curl_happy_path():
    """_post_via_curl parses a normal TG response and returns the dict."""
    fake_body = json.dumps({"ok": True, "result": {"message_id": 42}})
    with patch("tg_bridge.subprocess.run",
               return_value=_fake_completed_process(fake_body)) as mock_run:
        out = tg_bridge._post_via_curl(
            "sendMessage", {"chat_id": "1", "text": "hi"}
        )
    assert out == {"ok": True, "result": {"message_id": 42}}, out
    # Verify curl was invoked with -X POST + JSON header + our body.
    args = mock_run.call_args[0][0]
    assert args[0] == "curl"
    assert "-X" in args and "POST" in args
    assert "https://api.telegram.org/bot" + "test-token-for-mock-only" + "/sendMessage" in args
    assert "Content-Type: application/json" in args
    # The payload passed via -d should be valid JSON containing chat_id.
    d_idx = args.index("-d") + 1
    payload = json.loads(args[d_idx])
    assert payload["chat_id"] == "1" and payload["text"] == "hi"
    print("  curl happy path: OK")


def test_curl_rate_limit():
    """error_code=429 in the body → TGRateLimitError."""
    fake_body = json.dumps({
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests",
        "parameters": {"retry_after": 5},
    })
    with patch("tg_bridge.subprocess.run",
               return_value=_fake_completed_process(fake_body)):
        try:
            tg_bridge._post_via_curl("sendMessage", {"chat_id": "1"})
        except tg_bridge.TGRateLimitError as e:
            assert "429" in str(e)
            print("  curl rate-limit → TGRateLimitError: OK")
            return
    raise AssertionError("expected TGRateLimitError")


def test_curl_auth_error():
    """error_code=401 → TGAuthError."""
    fake_body = json.dumps({
        "ok": False,
        "error_code": 401,
        "description": "Unauthorized",
    })
    with patch("tg_bridge.subprocess.run",
               return_value=_fake_completed_process(fake_body)):
        try:
            tg_bridge._post_via_curl("sendMessage", {"chat_id": "1"})
        except tg_bridge.TGAuthError as e:
            assert "401" in str(e)
            print("  curl 401 → TGAuthError: OK")
            return
    raise AssertionError("expected TGAuthError")


def test_post_falls_back_to_urllib_on_curl_failure():
    """When curl raises a generic RuntimeError, _post must call urllib."""
    urllib_body = json.dumps({"ok": True, "result": {"message_id": 7}})
    fake_urllib_resp = MagicMock()
    fake_urllib_resp.read.return_value = urllib_body.encode("utf-8")
    fake_urllib_resp.__enter__ = lambda s: s
    fake_urllib_resp.__exit__ = MagicMock(return_value=False)

    with patch("tg_bridge.subprocess.run",
               return_value=_fake_completed_process("", returncode=7,
                                                   stderr="boom")), \
         patch("tg_bridge.urllib.request.urlopen",
               return_value=fake_urllib_resp) as mock_urlopen:
        out = tg_bridge._post("sendMessage", {"chat_id": "1", "text": "x"})
    assert out == {"ok": True, "result": {"message_id": 7}}, out
    assert mock_urlopen.called, "urllib fallback was not invoked"
    print("  curl fail → urllib fallback: OK")


def test_post_does_not_swallow_auth_error():
    """TGAuthError from curl must bubble up, not trigger urllib fallback."""
    fake_body = json.dumps({"ok": False, "error_code": 401})
    with patch("tg_bridge.subprocess.run",
               return_value=_fake_completed_process(fake_body)), \
         patch("tg_bridge.urllib.request.urlopen") as mock_urlopen:
        try:
            tg_bridge._post("sendMessage", {"chat_id": "1"})
        except tg_bridge.TGAuthError:
            pass
        else:
            raise AssertionError("expected TGAuthError to propagate")
    assert not mock_urlopen.called, (
        "urllib fallback should NOT run for TGAuthError"
    )
    print("  curl TGAuthError not swallowed: OK")


def main():
    print("== tg_bridge curl-fallback smoke ==")
    test_curl_happy_path()
    test_curl_rate_limit()
    test_curl_auth_error()
    test_post_falls_back_to_urllib_on_curl_failure()
    test_post_does_not_swallow_auth_error()
    print("OK")


if __name__ == "__main__":
    sys.exit(main() or 0)
