#!/usr/bin/env python3
"""Trust only explicitly rendered webhook routes behind the listener proxy.

Vicegerent authenticates public provider signatures in a dedicated listener and
allows only that listener pod to reach Hermes on port 8644. Upstream Hermes
requires a route secret even behind that network boundary, which would copy
signing material into the agent. This patch adds a boolean ``trusted_proxy``
route mode that requires the route secret to be absent, and preserves stable
PagerDuty and GitLab event metadata for filtering and retry deduplication.

Fail-loud on upstream drift and idempotent. Remove once upstream supports an
authenticated reverse-proxy boundary and nested provider delivery metadata.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


APPLIED_MARKER = "vicegerent-patch-0053"


def replace_once(source: str, old: str, new: str, path: Path) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"patch 0053: expected exactly 1 anchor in {path}, found {count}"
        )
    return source.replace(old, new)


def target_path() -> Path:
    override = os.environ.get("HERMES_WEBHOOK_PATH")
    if override:
        return Path(override)
    spec = importlib.util.find_spec("gateway.platforms.webhook")
    if spec is None or spec.origin is None:
        raise SystemExit("patch 0053: gateway.platforms.webhook not found")
    return Path(spec.origin)


def main() -> int:
    path = target_path()
    source = path.read_text(encoding="utf-8")
    if APPLIED_MARKER in source:
        print("0053: already applied")
        return 0

    source = replace_once(
        source,
        f"        # Validate routes at startup {chr(8212)} secret is required per route\n"
        + '''        for name, route in self._routes.items():
            secret = route.get("secret", self._global_secret)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
''',
        '''        # vicegerent-patch-0053: trusted_proxy routes are authenticated
        # by the dedicated listener and must never receive provider signing material.
        for name, route in self._routes.items():
            secret = route.get("secret", self._global_secret)
            trusted_proxy = route.get("trusted_proxy") is True
            if trusted_proxy and secret:
                raise ValueError(
                    f"[webhook] Route '{name}' cannot combine trusted_proxy with a secret."
                )
            if not trusted_proxy and not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
''',
        path,
    )
    source = replace_once(
        source,
        '''        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )
''',
        '''        secret = route_config.get("secret", self._global_secret)
        trusted_proxy = route_config.get("trusted_proxy") is True
        if trusted_proxy:
            if secret:
                logger.error(
                    "[webhook] Route %s combines trusted_proxy with a secret",
                    route_name,
                )
                return web.json_response(
                    {"error": "Webhook route authentication is misconfigured"},
                    status=403,
                )
        elif not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        elif secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )
''',
        path,
    )
    source = replace_once(
        source,
        '''        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
''',
        '''        nested_event = payload.get("event", {})
        if not isinstance(nested_event, dict):
            nested_event = {}
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or nested_event.get("event_type", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
''',
        path,
    )
    source = replace_once(
        source,
        '''        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )
''',
        '''        delivery_id = (
            request.headers.get("X-GitHub-Delivery", "")
            or request.headers.get("X-Gitlab-Event-UUID", "")
            or request.headers.get("X-Gitlab-Webhook-UUID", "")
            or request.headers.get("svix-id", "")
            or str(nested_event.get("id") or "")
            or request.headers.get("X-Request-ID", "")
            or f"body-sha256:{route_name}:{hashlib.sha256(raw_body).hexdigest()}"
        )
''',
        path,
    )

    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print("0053: trusted webhook proxy routes enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
