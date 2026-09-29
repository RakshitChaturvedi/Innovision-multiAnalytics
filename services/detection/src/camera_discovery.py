"""Which cameras does detection consume?

  1. DETECTION_CAMERA_IDS (comma separated) if set: static, never polled.
  2. Otherwise the platform camera registry (internal, no auth):
       GET {CAMERA_REGISTRY_URL}/cameras/by-uc/{SOURCE_UC}
       -> {"uc_id": "uc1", "camera_ids": ["<uuid>", ...]}

Only that response shape is accepted. Anything else is an error and the
caller keeps its current camera list.
"""

import asyncio
import json
import logging
import urllib.request
from urllib.parse import quote
from uuid import UUID

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """Base class: the caller keeps its current camera list."""


class RegistryUnavailable(RegistryError):
    """The registry could not be reached or answered with an error/junk body."""


class RegistryShapeError(RegistryError):
    """The registry answered, but not with {"uc_id", "camera_ids": [...]}."""


def _valid_ids(candidates, source: str) -> list[str]:
    ids: list[str] = []
    for raw in candidates:
        try:
            cam = str(UUID(str(raw).strip()))
        except (ValueError, AttributeError, TypeError):
            logger.warning("camera_id_invalid source=%s value=%r skipped", source, raw)
            continue
        if cam not in ids:
            ids.append(cam)
    return ids


def parse_static_camera_ids(csv: str) -> list[str]:
    return _valid_ids([p for p in csv.split(",") if p.strip()], "DETECTION_CAMERA_IDS")


def parse_registry_response(body) -> list[str]:
    if not isinstance(body, dict) or not isinstance(body.get("camera_ids"), list):
        raise RegistryShapeError(
            f"unexpected registry response shape (want "
            f"{{'uc_id': ..., 'camera_ids': [...]}}): {str(body)[:200]!r}"
        )
    return _valid_ids(body["camera_ids"], "registry")


def _http_get_json(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


async def fetch_registry_camera_ids(
    base_url: str, source_uc: str, timeout: float, get_json=_http_get_json
) -> list[str]:
    url = f"{base_url.rstrip('/')}/cameras/by-uc/{quote(source_uc, safe='')}"
    try:
        body = await asyncio.to_thread(get_json, url, timeout)
    except Exception as exc:  # network, HTTP status, timeout, invalid JSON
        raise RegistryUnavailable(f"GET {url} failed: {exc!r}") from exc
    return parse_registry_response(body)
