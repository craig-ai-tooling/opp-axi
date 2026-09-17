"""Client for the lm-42 tool gateway.

`LAWNMOWER_GATEWAY` unset is today's behaviour exactly: shell out to the local
`sf` CLI. That is what lets verbs migrate one at a time with nothing breaking,
and what makes one binary work on a laptop, on the VM and in a Ralph pod.

**Set, a gateway failure fails.** It does not quietly retry against the local
CLI. `docs/tool-gateway.md` in ai-lawnmower decided this and it is worth
restating, because "fall back to the local CLI" is easy to read as "fall back on
every error":

    LAWNMOWER_GATEWAY unset  ->  shell out locally, exactly as today
    LAWNMOWER_GATEWAY set    ->  call the gateway; on 5xx or timeout, say so and fail

A silent fallback would mean the gateway could be broken for a week while every
call quietly used a credential that was supposed to have moved, and nothing
would say so. The fallback is a deployment choice, made once by whether the
variable is set, not a per-call error handler.

stdlib only: opp-axi ships as a zipapp with no dependencies.
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_TOKEN_FILE = "~/.config/lawnmower/tool-gateway-token"


class GatewayError(Exception):
    """The gateway did not answer with records. Never an empty result."""


def endpoint():
    """The gateway base URL, or None for "use the local CLI"."""
    url = (os.environ.get("LAWNMOWER_GATEWAY") or "").strip()
    return url.rstrip("/") or None


def token():
    """The bearer token, from the env or from the file the cluster secret came
    from. Stripped: a file written with a trailing newline and a shell doing
    `$(cat ...)` disagree by one byte, and the symptom is a bare 401."""
    t = (os.environ.get("LAWNMOWER_GATEWAY_TOKEN") or "").strip()
    if t:
        return t
    path = os.path.expanduser(
        os.environ.get("LAWNMOWER_GATEWAY_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    )
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError as exc:
        raise GatewayError(
            f"LAWNMOWER_GATEWAY is set but no token was found: "
            f"LAWNMOWER_GATEWAY_TOKEN is unset and {path} is unreadable ({exc.strerror})"
        )


def sf_query(soql, timeout=180):
    """Run one SOQL SELECT through the gateway. Returns the record list.

    Raises GatewayError on every failure -- transport, auth, refusal or a
    malformed answer. It never returns [] to mean "something went wrong": the
    caller is entitled to read an empty list as "Salesforce matched nothing",
    which is the entire reason this service exists.
    """
    base = endpoint()
    if not base:
        raise GatewayError("no gateway configured")

    req = urllib.request.Request(
        f"{base}/sf/query",
        data=json.dumps({"soql": soql}).encode(),
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:400]
        except Exception:
            pass
        detail = body
        try:
            detail = json.loads(body)["error"]["message"]
        except Exception:
            pass
        raise GatewayError(f"gateway {base} refused ({exc.code}): {detail}")
    except urllib.error.URLError as exc:
        # The one this whole contract is about: the gateway did not answer.
        raise GatewayError(f"gateway {base} did not answer: {exc.reason}")
    except TimeoutError:
        raise GatewayError(f"gateway {base} timed out after {timeout}s")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise GatewayError(f"gateway {base} returned output that is not JSON")

    if not isinstance(payload, dict) or "records" not in payload:
        raise GatewayError(
            f"gateway {base} answered 200 without a records key; "
            "refusing to read that as an empty result"
        )
    return payload["records"]
