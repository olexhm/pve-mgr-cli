from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import AnyHttpUrl, Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pve_mgr.models._base import Model

Name = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class TransportKind(StrEnum):
    SSH = "ssh"
    API = "api"


class Node(Model):
    """How to reach one Proxmox node. The key in `nodes` is the real node name."""

    ssh: Name | None = Field(default=None, description="SSH target, e.g. root@pve1")
    api: AnyHttpUrl | None = Field(default=None, description="API base URL, e.g. https://pve1:8006")

    @model_validator(mode="before")
    @classmethod
    def _ssh_shorthand(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"ssh": value}
        return value


class Defaults(Model):
    """Values used when a device does not set its own."""

    builder_node: Name | None = None
    host_node: Name | None = None
    disk_storage: Name | None = None
    migration_storage: Name | None = None


class Config(Model):
    transport: TransportKind = TransportKind.SSH
    nodes: dict[Name, Node] = Field(min_length=1)
    entry_node: Name | None = Field(
        default=None,
        description="Node every call goes through; defaults to defaults.host_node",
    )
    sudo: bool | None = Field(
        default=None,
        description="Run pvesh through sudo; by default only when the SSH target is not root@...",
    )
    defaults: Defaults = Defaults()
    templates: dict[Name, PositiveInt] = Field(
        default_factory=dict, description="Template name -> VMID"
    )
    inventory: Path = Path("inventory.ini")
    known_hosts: Path = Path("~/.ssh/known_hosts")
    verify_tls: bool = True

    @model_validator(mode="after")
    def _check_nodes(self) -> Self:
        for field in ("builder_node", "host_node"):
            node = getattr(self.defaults, field)
            if node is not None and node not in self.nodes:
                raise ValueError(f"defaults.{field}: unknown node '{node}'")

        if self.defaults.host_node is None and len(self.nodes) > 1:
            raise ValueError("defaults.host_node is required when more than one node is defined")

        if self.entry_node is not None and self.entry_node not in self.nodes:
            raise ValueError(f"entry_node: unknown node '{self.entry_node}'")

        if self.endpoint(self.nodes[self.entry]) is None:
            raise ValueError(
                f"transport is '{self.transport}' but entry node '{self.entry}' "
                f"has no '{self.transport}' endpoint"
            )
        return self

    def endpoint(self, node: Node) -> str | AnyHttpUrl | None:
        return node.ssh if self.transport is TransportKind.SSH else node.api

    @property
    def entry(self) -> str:
        return self.entry_node or self.default_host_node

    @property
    def use_sudo(self) -> bool:
        if self.sudo is not None:
            return self.sudo
        target = self.nodes[self.entry].ssh or ""
        return not target.startswith("root@")

    @property
    def default_host_node(self) -> str:
        return self.defaults.host_node or next(iter(self.nodes))

    @property
    def default_builder_node(self) -> str:
        return self.defaults.builder_node or self.default_host_node

    @property
    def default_migration_storage(self) -> str | None:
        return self.defaults.migration_storage or self.defaults.disk_storage


class Secrets(BaseSettings):
    """Credentials, read from the environment or `.env` only — never from config files."""

    model_config = SettingsConfigDict(env_prefix="PVE_MGR_", env_file=".env", extra="ignore")

    api_token_id: str | None = Field(default=None, description="e.g. automation@pve!pve-mgr")
    api_token_secret: SecretStr | None = None
