"""
RidgeWalker LINZ proxy — AWS Lambda handler.

Purpose:
    Keeps the LINZ API key entirely server-side. The browser never sees it.
    Every request MapLibre makes for a tile, sprite, glyph, or nested
    TileJSON document is redirected (client-side, via MapLibre's
    transformRequest hook — see MapLogic.js) to THIS function instead of
    basemaps.linz.govt.nz directly. This function appends the real key
    and forwards the request to LINZ, then relays the response back.

    Instead: strip any literal occurrence of the key from JSON
    response bodies as plain text (so nothing is ever visible even
    momentarily), and let MapLibre's transformRequest reroute every
    *subsequent* request regardless of what URL shape LINZ used.

API Gateway:
    Only ONE route needed: GET /proxy/{proxy+}
"""

import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse

LINZ_BASE = "https://basemaps.linz.govt.nz/v1"

_cached_key = None  # reused across warm Lambda invocations


def get_linz_key():
    global _cached_key
    if _cached_key:
        return _cached_key

    env_key = os.environ.get("LINZ_API_KEY")
    if env_key:
        _cached_key = env_key
        return _cached_key

    import boto3
    client = boto3.client("secretsmanager")
    secret_id = os.environ.get("LINZ_SECRET_ID", "ridgewalker/linz-api-key")
    response = client.get_secret_value(SecretId=secret_id)
    secret = json.loads(response["SecretString"])
    _cached_key = secret["LINZ_API_KEY"]
    return _cached_key


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "RidgeWalker-Proxy/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, dict(resp.getheaders()), resp.read()


def lambda_handler(event, context):
    key = get_linz_key()
    raw_path = event.get("rawPath") or event.get("path", "")

    if "/proxy/" not in raw_path:
        return {"statusCode": 404, "body": "Not found"}

    linz_path = raw_path.split("/proxy/", 1)[1]
    linz_path = urllib.parse.quote(
        urllib.parse.unquote(linz_path),
        safe="/"
    )
    qs = event.get("rawQueryString", "")
    joiner = "&" if qs else ""
    url = f"{LINZ_BASE}/{linz_path}?{qs}{joiner}api={key}"

    try:
        status, headers, body = _fetch(url)
    except urllib.error.HTTPError as e:
        return {"statusCode": e.code, "body": f"LINZ upstream error: {e.reason}"}

    content_type = headers.get("Content-Type", "application/octet-stream")

    if "json" in content_type.lower():
        # Plain-text key strip — works no matter how deeply the key is
        # nested in the JSON structure, since we're not relying on
        # understanding the shape of LINZ's response at all.
        text = body.decode("utf-8")
        text = text.replace(f"?api={key}", "").replace(f"&api={key}", "")
        return {
            "statusCode": status,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "public, max-age=86400",
            },
            "body": text,
        }

    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        },
        "body": base64.b64encode(body).decode("utf-8"),
        "isBase64Encoded": True,
    }
