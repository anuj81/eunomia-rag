"""Layered settings loader for eunomia-rag.

Precedence (highest wins):
    1. CLI overrides   — via load_settings(cli_overrides={...})
    2. Environment     — EUNOMIA_RAG_<SECTION>__<FIELD>, or well-known short
                         names (OPENMETADATA_PASSWORD, RAG_API_KEY)
    3. YAML file       — config/eunomia-rag.yaml; override via $EUNOMIA_RAG_CONFIG
                         or the --config CLI flag
    4. Built-in defaults

Secret fields ({openmetadata.password, auth.api_key}) are rejected if present
in YAML.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# --------------------------------------------------------------------------- #
# Per-section schemas                                                         #
# --------------------------------------------------------------------------- #

LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class LoggingRotationConfig(BaseModel):
    max_bytes: int = 10_485_760
    backup_count: int = 5


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    log_dir: Path = Path("./logs")
    console: bool = True
    file: bool = True
    rotation: LoggingRotationConfig = Field(default_factory=LoggingRotationConfig)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9000
    reload: bool = True


class OpenMetadataConfig(BaseModel):
    url: str = "http://localhost:8585/api/v1"
    username: str = "admin@open-metadata.org"
    database_fqn: str = "zenith_mysql.zenith_corp_eunomia.zenith_corp_eunomia"
    password: Optional[str] = None  # env-only


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    collection: str = "eunomia_views"
    vector_size: int = 384
    distance: Literal["Cosine", "Dot", "Euclid"] = "Cosine"


class EmbeddingConfig(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    preload: bool = True


class AuthConfig(BaseModel):
    require_auth: bool = True
    api_key: Optional[str] = None  # env-only


# --------------------------------------------------------------------------- #
# Top-level Settings                                                          #
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EUNOMIA_RAG_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    openmetadata: OpenMetadataConfig = Field(default_factory=OpenMetadataConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)


# --------------------------------------------------------------------------- #
# Loader internals                                                            #
# --------------------------------------------------------------------------- #


class SecretInYamlError(ValueError):
    """Raised when the YAML config contains a field that must come from env."""


_FORBIDDEN_YAML_SECRETS: Tuple[Tuple[str, str], ...] = (
    ("openmetadata", "password"),
    ("auth", "api_key"),
)

_WELL_KNOWN_SECRET_ENV: Dict[str, Tuple[str, str]] = {
    "OPENMETADATA_PASSWORD": ("openmetadata", "password"),
    "RAG_API_KEY": ("auth", "api_key"),
}


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML config at {path} must be a mapping at the root.")
    return data


def _assert_no_yaml_secrets(yaml_data: Dict[str, Any], path: Path) -> None:
    violations = []
    for section, field in _FORBIDDEN_YAML_SECRETS:
        section_data = yaml_data.get(section)
        if isinstance(section_data, dict) and section_data.get(field) is not None:
            violations.append(f"{section}.{field}")
    if violations:
        raise SecretInYamlError(
            f"Secrets present in YAML config ({path}): {violations}. "
            "Use environment variables: OPENMETADATA_PASSWORD, RAG_API_KEY."
        )


def _overlay_well_known_secrets(yaml_data: Dict[str, Any]) -> None:
    for env_key, (section, field) in _WELL_KNOWN_SECRET_ENV.items():
        val = os.environ.get(env_key)
        if val:
            yaml_data.setdefault(section, {})[field] = val


def _overlay_prefixed_env(yaml_data: Dict[str, Any]) -> None:
    prefix = "EUNOMIA_RAG_"
    delim = "__"
    for env_key, value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        remainder = env_key[len(prefix):]
        if delim not in remainder:
            continue
        path_parts = [p.lower() for p in remainder.split(delim)]
        cursor = yaml_data
        for part in path_parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path_parts[-1]] = value


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {**base}
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _default_config_path() -> Path:
    env_path = os.environ.get("EUNOMIA_RAG_CONFIG")
    if env_path:
        return Path(env_path)
    project_root = Path(__file__).resolve().parents[2]  # .../eunomia-rag
    return project_root / "config" / "eunomia-rag.yaml"


def load_settings(
    config_path: Optional[Path] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> Settings:
    path = Path(config_path) if config_path else _default_config_path()
    yaml_data = _read_yaml(path)
    _assert_no_yaml_secrets(yaml_data, path)
    _overlay_well_known_secrets(yaml_data)
    _overlay_prefixed_env(yaml_data)
    if cli_overrides:
        yaml_data = _deep_merge(yaml_data, cli_overrides)
    return Settings(**yaml_data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
