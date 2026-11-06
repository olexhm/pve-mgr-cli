"""What Proxmox tells us. Lenient on purpose: it adds fields between versions."""

from pydantic import BaseModel, ConfigDict

from pve_mgr.models.device import GuestType


class Reply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Guest(Reply):
    """One entry of `/cluster/resources?type=vm`."""

    vmid: int
    type: GuestType
    node: str
    status: str
    name: str | None = None
    pool: str | None = None
    template: bool = False

    @property
    def ref(self) -> str:
        """The ID Proxmox uses for this guest in pools and resource lists."""
        return f"{self.type}/{self.vmid}"

    @property
    def running(self) -> bool:
        return self.status == "running"
