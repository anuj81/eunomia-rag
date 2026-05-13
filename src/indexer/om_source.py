"""OpenMetadata REST source for the indexer.

Returns a list of normalized "table descriptors" (dicts) shaped for the doc
synthesizer. Same login flow as eunomia-middleware's catalog client.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

import requests

from ..config import Settings

logger = logging.getLogger(__name__)


class OpenMetadataSource:
    """Read-only OM REST client used at indexing time."""

    def __init__(self, settings: Settings):
        cfg = settings.openmetadata
        self._url = cfg.url.rstrip("/")
        self._username = cfg.username
        self._password = cfg.password
        self._database_fqn = cfg.database_fqn
        self._token: Optional[str] = None
        self._session = requests.Session()

    # --------------------------------------------------------------- auth #

    def _login(self) -> None:
        if self._token is not None:
            return
        if not self._password:
            logger.warning(
                "openmetadata.password not set (OPENMETADATA_PASSWORD). "
                "Indexer requests will likely 401."
            )
            return
        try:
            pwd_b64 = base64.b64encode(self._password.encode()).decode()
            resp = self._session.post(
                f"{self._url}/users/login",
                json={"email": self._username, "password": pwd_b64},
                timeout=10,
            )
            if resp.status_code == 200:
                self._token = resp.json().get("accessToken")
                logger.info("OpenMetadata login succeeded as %s", self._username)
            else:
                logger.error(
                    "OpenMetadata login failed: %s %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("OpenMetadata login raised")

    def _headers(self) -> Dict[str, str]:
        self._login()
        if not self._token:
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------- public read #

    def fetch_tables(self) -> List[Dict[str, Any]]:
        """Fetch all tables under the configured database FQN.

        Returns a list of normalized dicts ready for doc synthesis. Pages
        through OpenMetadata's cursor-based pagination.
        """
        page_size = 100
        out: List[Dict[str, Any]] = []
        after: Optional[str] = None

        while True:
            params: Dict[str, Any] = {
                "database": self._database_fqn,
                # OM 1.12 expects plural `domains` and `owners`; singular forms 400.
                "fields": "columns,tags,owners,domains",
                "limit": page_size,
            }
            if after:
                params["after"] = after
            try:
                resp = self._session.get(
                    f"{self._url}/tables",
                    params=params,
                    headers=self._headers(),
                    timeout=15,
                )
                resp.raise_for_status()
            except Exception:
                logger.exception(
                    "OpenMetadata fetch failed (database=%s, after=%s)",
                    self._database_fqn, after,
                )
                break

            body = resp.json() or {}
            for t in body.get("data", []) or []:
                out.append(_normalize_table(t))

            paging = body.get("paging") or {}
            after = paging.get("after")
            if not after:
                break

        logger.info(
            "OM fetched %d tables under %s", len(out), self._database_fqn,
        )
        return out


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


def _normalize_table(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the few fields we care about; flatten owners/domains.

    OM 1.x returns:
        owners:  list of EntityReference dicts (may be empty)
        domains: list of EntityReference dicts (may be empty)
    Older code paths used singular `domain` (an object) and `owner` — we
    accept both for forward/backward compatibility.
    """
    # Owners: prefer first entry's name; both shapes seen in the wild.
    owners = raw.get("owners")
    if isinstance(owners, list) and owners:
        owner_name = owners[0].get("name")
    elif isinstance(raw.get("owner"), dict):
        owner_name = raw["owner"].get("name")
    else:
        owner_name = None

    # Domain: same dual-shape tolerance.
    domains = raw.get("domains")
    if isinstance(domains, list) and domains:
        domain_name = domains[0].get("name")
    elif isinstance(raw.get("domain"), dict):
        domain_name = raw["domain"].get("name")
    else:
        domain_name = None

    columns: List[Dict[str, str]] = []
    for c in raw.get("columns") or []:
        name = c.get("name")
        if not name:
            continue
        columns.append({
            "name": name,
            "description": (c.get("description") or "").strip(),
            "data_type": (c.get("dataType") or c.get("dataTypeDisplay") or ""),
        })

    return {
        "name": raw.get("name") or "",
        "fqn": raw.get("fullyQualifiedName") or "",
        "description": (raw.get("description") or "").strip(),
        "domain": domain_name,
        "owner_team": owner_name,
        "columns": columns,
    }
