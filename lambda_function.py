"""
RidgeWalker LINZ proxy — AWS Lambda handler.

Keeps the LINZ API key server-side and forwards MapLibre requests
to LINZ through API Gateway.

API Gateway route:
GET /proxy/{proxy+}
"""

import os
import json
import base64
import urllib.request
import urllib.error
import urllib.parse


LINZ_BASE = "https://basemaps.linz.govt.nz/v1"

# "*" allows both the deployed website and localhost during marking.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

_cached_key = None


def get_linz_key():
    global _cached_key

    if _cached_key:
        return _cached_key

    # First try the Lambda environment variable.
    env_key = os.environ.get("LINZ_API_KEY")

    if env_key:
        _cached_key = env_key
        return _cached_key

    # Otherwise retrieve the key from AWS Secrets Manager.
    import boto3

    client = boto3.client("secretsmanager")
    secret_id = os.environ.get(
        "LINZ_SECRET_ID",
        "ridgewalker/linz-api-key"
    )

    response = client.get_secret_value(SecretId=secret_id)
    secret = json.loads(response["SecretString"])

    _cached_key = secret["LINZ_API_KEY"]
    return _cached_key


def _fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RidgeWalker-Proxy/1.0"}
    )

    with urllib.request.urlopen(request, timeout=8) as response:
        return (
            response.status,
            dict(response.getheaders()),
            response.read()
        )


def lambda_handler(event, context):
    # Supports both HTTP API v2 and REST API v1 event formats.
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod")
        or "GET"
    )

    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": CORS_HEADERS,
            "body": ""
        }

    try:
        key = get_linz_key()

        raw_path = event.get("rawPath") or event.get("path", "")

        if "/proxy/" not in raw_path:
            return {
                "statusCode": 404,
                "headers": CORS_HEADERS,
                "body": "Not found"
            }

        linz_path = raw_path.split("/proxy/", 1)[1]

        # Decode and safely re-encode the requested LINZ path.
        linz_path = urllib.parse.quote(
            urllib.parse.unquote(linz_path),
            safe="/"
        )

        query_string = event.get("rawQueryString", "")

        # REST API v1 fallback.
        if not query_string:
            query_parameters = (
                event.get("queryStringParameters") or {}
            )
            query_string = urllib.parse.urlencode(query_parameters)

        joiner = "&" if query_string else ""

        url = (
            f"{LINZ_BASE}/{linz_path}"
            f"?{query_string}{joiner}api={urllib.parse.quote(key)}"
        )

        status, upstream_headers, body = _fetch(url)

    except urllib.error.HTTPError as error:
        return {
            "statusCode": error.code,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": "text/plain"
            },
            "body": f"LINZ upstream error: {error.reason}"
        }

    except Exception as error:
        return {
            "statusCode": 502,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": "text/plain"
            },
            "body": f"Proxy error: {error}"
        }

    headers_ci = {
        name.lower(): value
        for name, value in upstream_headers.items()
    }

    content_type = headers_ci.get(
        "content-type",
        "application/octet-stream"
    )

    if "json" in content_type.lower():
        text = body.decode("utf-8")

        # Remove the LINZ key if LINZ includes it in JSON URLs.
        encoded_key = urllib.parse.quote(key)

        text = text.replace(f"?api={key}", "")
        text = text.replace(f"&api={key}", "")
        text = text.replace(f"?api={encoded_key}", "")
        text = text.replace(f"&api={encoded_key}", "")

        return {
            "statusCode": status,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": content_type,
                "Cache-Control": "public, max-age=86400"
            },
            "body": text
        }

    response_headers = {
        **CORS_HEADERS,
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=86400"
    }

    # Only return Content-Encoding if LINZ actually supplied it.
    content_encoding = headers_ci.get("content-encoding")

    if content_encoding:
        response_headers["Content-Encoding"] = content_encoding

    return {
        "statusCode": status,
        "headers": response_headers,
        "body": base64.b64encode(body).decode("utf-8"),
        "isBase64Encoded": True
    }