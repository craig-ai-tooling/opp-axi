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

from opp_axi import __version__

DEFAULT_TOKEN_FILE = "~/.config/lawnmower/tool-gateway-token"
DEFAULT_CF_ACCESS_FILE = "~/.config/lawnmower/tool-gateway-cf-access"

# Not cosmetic. Cloudflare's Browser Integrity Check blocks `Python-urllib/*`
# outright: the gateway's public hostname answers 403 "error code: 1010" before
# Access or the gateway sees the request, and a valid service token does not
# save you. Measured 9/18/26 -- curl/8 and this string both get 200 on the same
# request that Python-urllib/3.12 gets 403 for. Any AXI that talks to a
# Cloudflare-proxied host needs a User-Agent of its own.
USER_AGENT = f"opp-axi/{__version__} (+lm-42 tool-gateway client)"


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


def cf_access_headers():
    """Cloudflare Access service-token headers, or {} when none is configured.

    The public hostname sits behind an Access application, so a machine caller
    has to present a service token or Cloudflare answers the login redirect
    instead of the gateway. Absent, this returns {} and a direct in-cluster call
    still works -- Access is at the edge, not on the Service.

    A missing credential is NOT an error here. It is only an error to the extent
    that Cloudflare then refuses, and `sf_query` names that case specifically so
    the 302 does not read as a gateway fault.
    """
    cid = (os.environ.get("CF_ACCESS_CLIENT_ID") or "").strip()
    sec = (os.environ.get("CF_ACCESS_CLIENT_SECRET") or "").strip()
    if not (cid and sec):
        path = os.path.expanduser(
            os.environ.get("LAWNMOWER_GATEWAY_CF_ACCESS_FILE") or DEFAULT_CF_ACCESS_FILE
        )
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == "CF_ACCESS_CLIENT_ID" and not cid:
                        cid = v
                    elif k == "CF_ACCESS_CLIENT_SECRET" and not sec:
                        sec = v
        except OSError:
            return {}
    if cid and sec:
        return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": sec}
    return {}


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
            "User-Agent": USER_AGENT,
            **cf_access_headers(),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            final_url = resp.geturl()
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
        if exc.code in (301, 302, 303) or "cloudflareaccess.com" in (
            exc.headers.get("Location") or ""
        ):
            raise GatewayError(
                f"gateway {base} answered a Cloudflare Access login redirect, which "
                "means no valid service token was sent. Set CF_ACCESS_CLIENT_ID and "
                f"CF_ACCESS_CLIENT_SECRET, or put them in {DEFAULT_CF_ACCESS_FILE}."
            )
        if exc.code == 403 and "1010" in body:
            raise GatewayError(
                f"gateway {base} was blocked by Cloudflare's Browser Integrity "
                f"Check (error 1010), which rejects the request on its User-Agent "
                f"before Access or the gateway sees it. Expected UA: {USER_AGENT!r}."
            )
        raise GatewayError(f"gateway {base} refused ({exc.code}): {detail}")
    except urllib.error.URLError as exc:
        # The one this whole contract is about: the gateway did not answer.
        raise GatewayError(f"gateway {base} did not answer: {exc.reason}")
    except TimeoutError:
        raise GatewayError(f"gateway {base} timed out after {timeout}s")

    # urlopen FOLLOWS the Access login redirect, so a missing service token
    # arrives as a 200 full of HTML rather than as an HTTPError. Reported as
    # "output that is not JSON" it reads like the gateway misbehaving, when the
    # request never reached it.
    if "cloudflareaccess.com" in (final_url or ""):
        raise GatewayError(
            f"gateway {base} redirected to a Cloudflare Access login, which means "
            "no valid service token was sent. Set CF_ACCESS_CLIENT_ID and "
            f"CF_ACCESS_CLIENT_SECRET, or put them in {DEFAULT_CF_ACCESS_FILE}."
        )

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
