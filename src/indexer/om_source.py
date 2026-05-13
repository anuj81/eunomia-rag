"""OpenMetadata REST source for the indexer.

Returns a list of normalized "table descriptors" (dicts) shaped for the doc
synthesizer.

Auth strategy:
    1. Phase D / preferred — Keycloak `client_credentials` grant.
       Requires settings.keycloak.client_id + client_secret. Token has the
       `eunomia-om-admin` realm role (set on the service-account user) so OM
       grants full read access.
    2. Legacy fallback — OpenMetadata `/users/login` with username/password.
       Used only if Keycloak config is missing.
"""

from __future__ import annotations

import base64
import logging
import time
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

        kc = settings.keycloak
        self._kc_token_endpoint = kc.issuer.rstrip("/") + "/protocol/openid-connect/token"
        self._kc_client_id = kc.client_id
        self._kc_client_secret = kc.client_secret
        self._kc_refresh_window = kc.refresh_window_seconds

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()

    # --------------------------------------------------------------- auth #

    def _login(self) -> None:
        """Acquire (or refresh) an access token.

        Phase D path: Keycloak `client_credentials` grant.
        Legacy path:  OM basic-auth.

        Cached for the token's lifetime minus a refresh window.
        """
        # Cached and still fresh?
        if self._token and time.time() < self._token_expires_at - self._kc_refresh_window:
            return

        if self._kc_client_secret:
            self._login_via_keycloak()
            return

        # Legacy fallback — only meaningful if OM is in `basic` or `multi`.
        self._login_via_om_basic()

    def _login_via_keycloak(self) -> None:
        try:
            resp = self._session.post(
                self._kc_token_endpoint,
                data={
                    "client_id":     self._kc_client_id,
                    "client_secret": self._kc_client_secret,
                    "grant_type":    "client_credentials",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(
                    "Keycloak client_credentials grant failed: %s %s",
                    resp.status_code, resp.text[:200],
                )
                return
            body = resp.json()
            self._token = body.get("access_token")
            # `expires_in` is seconds-until-expiry from now.
            self._token_expires_at = time.time() + int(body.get("expires_in", 60))
            logger.info(
                "Keycloak token acquired for client_id=%s (expires_in=%ss)",
                self._kc_client_id, body.get("expires_in"),
            )
        except Exception:
            logger.exception("Keycloak client_credentials grant raised")

    def _login_via_om_basic(self) -> None:
        if not self._password:
            logger.warning(
                "Neither KEYCLOAK_RAG_INDEXER_SECRET nor OPENMETADATA_PASSWORD "
                "is set — OM requests will 401."
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
                # OM tokens default to ~24h; set a conservative cache TTL.
                self._token_expires_at = time.time() + 3600
                logger.info(
                    "OpenMetadata basic-login succeeded as %s (legacy path)",
                    self._username,
                )
            else:
                logger.error(
                    "OpenMetadata basic-login failed: %s %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("OpenMetadata basic-login raised")

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
    """Pick the few fields we care about; flatten owners/domains + PII tags.

    Phase D additions:
        • `pii_columns` — list of column names tagged `PII.Sensitive`.
        • Per-column `tags` list — preserved on each column for downstream
          consumers (e.g. middleware reading PII sidecar from RAG retrieve
          payload — wired in #23 part 2).

    OM 1.x returns:
        owners:  list of EntityReference dicts (may be empty)
        domains: list of EntityReference dicts (may be empty)
    Older code paths used singular `domain` (an object) and `owner` — we
    accept both for forward/backward compatibility.
    """
    # Owners
    owners = raw.get("owners")
    if isinstance(owners, list) and owners:
        owner_name = owners[0].get("name")
    elif isinstance(raw.get("owner"), dict):
        owner_name = raw["owner"].get("name")
    else:
        owner_name = None

    # Domain
    domains = raw.get("domains")
    if isinstance(domains, list) and domains:
        domain_name = domains[0].get("name")
    elif isinstance(raw.get("domain"), dict):
        domain_name = raw["domain"].get("name")
    else:
        domain_name = None

    columns: List[Dict[str, Any]] = []
    pii_columns: List[str] = []
    for c in raw.get("columns") or []:
        name = c.get("name")
        if not name:
            continue
        tag_fqns = [t.get("tagFQN") for t in (c.get("tags") or []) if t.get("tagFQN")]
        is_pii = any(t == "PII.Sensitive" for t in tag_fqns)
        if is_pii:
            pii_columns.append(name)
        columns.append({
            "name": name,
            "description": (c.get("description") or "").strip(),
            "data_type": (c.get("dataType") or c.get("dataTypeDisplay") or ""),
            "tags": tag_fqns,
            "is_pii": is_pii,
        })

    return {
        "name": raw.get("name") or "",
        "fqn": raw.get("fullyQualifiedName") or "",
        "description": (raw.get("description") or "").strip(),
        "domain": domain_name,
        "owner_team": owner_name,
        "columns": columns,
        "pii_columns": pii_columns,
    }
