import re
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BeforeValidator, Field, NonNegativeInt, PositiveInt, model_validator

from pve_mgr.models._base import Model
from pve_mgr.models.config import Name

_DISK_SIZE = re.compile(r"([1-9]\d*)\s*([GT])?(?:I?B)?", re.IGNORECASE)


def _normalize_disk_size(value: Any) -> Any:
    """Accept 20, "20", "20G", "20GiB" or "2T" and return "20G" / "2T"."""
    match = _DISK_SIZE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"expected a whole size such as 20G or 2T, got '{value}'")
    amount, unit = match.groups()
    return f"{amount}{(unit or 'G').upper()}"


DiskSize = Annotated[str, BeforeValidator(_normalize_disk_size)]
DiskSlot = Annotated[
    str,
    Field(pattern=r"^(ide[1-3]|sata[1-5]|scsi([1-9]|[12]\d|30)|virtio([1-9]|1[0-5]))$"),
]
IsoImage = Annotated[
    str, Field(pattern=r"^[^\s:,]+:iso/[^\s,]+$", description="e.g. local:iso/debian-13.iso")
]
Hostname = Annotated[str, Field(max_length=63, pattern=r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")]
Tag = Annotated[str, Field(pattern=r"^[a-z0-9_][a-z0-9_+.-]*$")]
ProxyJump = Annotated[str, Field(min_length=1, pattern=r"^[^\s,]+$")]
VmId = Annotated[int, Field(ge=100, le=999_999_999)]
CpuType = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.+-]+$", description="e.g. host")]
StartupOrder = Annotated[
    str,
    Field(
        pattern=r"^(order|up|down)=\d+(,(order|up|down)=\d+)*$",
        description="e.g. order=2,up=30",
    ),
]
OptionKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
Port = Annotated[int, Field(ge=1, le=65535)]


class GuestType(StrEnum):
    QEMU = "qemu"
    LXC = "lxc"


class Role(StrEnum):
    GATEWAY = "gateway"


class OsType(StrEnum):
    OTHER = "other"
    LINUX_24 = "l24"
    LINUX_26 = "l26"
    SOLARIS = "solaris"
    WINDOWS_XP = "wxp"
    WINDOWS_2000 = "w2k"
    WINDOWS_2003 = "w2k3"
    WINDOWS_2008 = "w2k8"
    WINDOWS_VISTA = "wvista"
    WINDOWS_7 = "win7"
    WINDOWS_8 = "win8"
    WINDOWS_10 = "win10"
    WINDOWS_11 = "win11"


class Bios(StrEnum):
    SEABIOS = "seabios"
    OVMF = "ovmf"


class Machine(StrEnum):
    PC = "pc"
    Q35 = "q35"


class ScsiController(StrEnum):
    LSI = "lsi"
    LSI_53C810 = "lsi53c810"
    MEGASAS = "megasas"
    PVSCSI = "pvscsi"
    VIRTIO_SCSI_PCI = "virtio-scsi-pci"
    VIRTIO_SCSI_SINGLE = "virtio-scsi-single"


class DiskBus(StrEnum):
    SCSI = "scsi"
    VIRTIO = "virtio"
    SATA = "sata"
    IDE = "ide"


class DiskCache(StrEnum):
    NONE = "none"
    WRITETHROUGH = "writethrough"
    WRITEBACK = "writeback"
    DIRECTSYNC = "directsync"
    UNSAFE = "unsafe"


class TpmVersion(StrEnum):
    V1_2 = "v1.2"
    V2_0 = "v2.0"


class NicModel(StrEnum):
    VIRTIO = "virtio"
    E1000 = "e1000"
    E1000E = "e1000e"
    VMXNET3 = "vmxnet3"
    RTL8139 = "rtl8139"


class Vga(StrEnum):
    STD = "std"
    CIRRUS = "cirrus"
    VMWARE = "vmware"
    QXL = "qxl"
    VIRTIO = "virtio"
    VIRTIO_GL = "virtio-gl"
    SERIAL0 = "serial0"
    NONE = "none"


class _DiskSettings(Model):
    size: DiskSize
    storage: Name | None = Field(default=None, description="Defaults to defaults.disk_storage")
    ssd: bool | None = Field(default=None, description="Present the disk as an SSD")
    discard: bool | None = Field(default=None, description="Pass TRIM on to the storage")
    iothread: bool | None = None
    cache: DiskCache | None = None

    @property
    def size_gib(self) -> int:
        amount = int(self.size[:-1])
        return amount * 1024 if self.size.endswith("T") else amount

    def options(self) -> tuple[str, ...]:
        """The written settings as the parts of a Proxmox drive string."""
        parts = {
            "ssd": None if self.ssd is None else int(self.ssd),
            "discard": None if self.discard is None else ("on" if self.discard else "ignore"),
            "iothread": None if self.iothread is None else int(self.iothread),
            "cache": None if self.cache is None else self.cache.value,
        }
        return tuple(f"{key}={value}" for key, value in parts.items() if value is not None)

    def _check_bus(self, slot: str) -> None:
        if self.ssd is not None and slot.startswith("virtio"):
            raise ValueError("ssd: not supported on a virtio disk")
        if self.iothread is not None and not slot.startswith(("scsi", "virtio")):
            raise ValueError("iothread: only supported on scsi and virtio disks")


class BootDiskSpec(_DiskSettings):
    bus: DiskBus = DiskBus.SCSI

    @property
    def slot(self) -> str:
        return f"{self.bus}0"

    @model_validator(mode="after")
    def _check_settings(self) -> Self:
        self._check_bus(self.slot)
        return self


class BootDisk(BootDiskSpec):
    storage: Name


class DiskSpec(_DiskSettings):
    slot: DiskSlot = "scsi1"

    @model_validator(mode="after")
    def _check_settings(self) -> Self:
        self._check_bus(self.slot)
        return self


class Disk(DiskSpec):
    storage: Name


class IsoSpec(Model):
    """An empty guest that boots an installer image, instead of a clone of a template."""

    image: IsoImage
    disk: BootDiskSpec = Field(description="The disk to install to, slot 0 of its bus")
    tpm: TpmVersion | None = Field(default=None, description="Add a TPM of this version")


class Iso(IsoSpec):
    disk: BootDisk


class NicSpec(Model):
    """Settings shared by every NIC made from `bridges`."""

    model: NicModel | None = Field(default=None, description="Default: virtio")
    firewall: bool | None = Field(default=None, description="Default: off")
    mtu: Annotated[int, Field(ge=1, le=65520)] | None = Field(
        default=None, description="virtio only; 1 takes the bridge's MTU"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.mtu is not None and self.model not in (None, NicModel.VIRTIO):
            raise ValueError("mtu: only supported with the virtio model")
        return self


_HARDWARE_KEYS = {"cpu_limit": "cpulimit", "cpu_units": "cpuunits", "balloon_mb": "balloon"}
_OWNED_OPTIONS = frozenset(
    {"vmid", "name", "pool", "cores", "memory", "tags", "agent", "boot", "delete", "revert"}
)


class Hardware(Model):
    """QEMU settings. One that is not written is left as the template or Proxmox has it."""

    ostype: OsType | None = Field(default=None, description="With iso, defaults to l26")
    bios: Bios | None = Field(
        default=None, description="With iso, defaults to seabios; ovmf adds an EFI disk"
    )
    machine: Machine | None = Field(default=None, description="pc is i440fx")
    scsihw: ScsiController | None = Field(
        default=None, description="With iso, defaults to virtio-scsi-single"
    )
    cpu: CpuType | None = None
    sockets: PositiveInt | None = None
    numa: bool | None = None
    cpu_limit: Annotated[float, Field(ge=0, le=128)] | None = Field(
        default=None, description="Cores' worth of CPU time; 0 is unlimited"
    )
    cpu_units: Annotated[int, Field(ge=1, le=262144)] | None = None
    balloon_mb: NonNegativeInt | None = Field(
        default=None, description="Minimum memory; 0 turns ballooning off"
    )
    vga: Vga | None = None
    onboot: bool | None = Field(default=None, description="Start with the node")
    startup: StartupOrder | None = None
    protection: bool | None = None
    tablet: bool | None = None
    kvm: bool | None = None
    description: str | None = None
    nic: NicSpec | None = None
    options: dict[OptionKey, str | int | bool] = Field(
        default_factory=dict,
        description="Any other Proxmox setting by its own name, sent as written",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        typed = {_HARDWARE_KEYS.get(name, name): name for name in self._settings()}
        for key in self.options:
            if key in typed:
                raise ValueError(f"options.{key}: use {typed[key]}")
            if key in _OWNED_OPTIONS:
                raise ValueError(f"options.{key}: set by pve-mgr from the device")
        return self

    @classmethod
    def _settings(cls) -> list[str]:
        return [name for name in cls.model_fields if name not in ("nic", "options")]

    def params(self) -> dict[str, str | int | bool]:
        """Every written setting under Proxmox's name for it, options last."""
        params: dict[str, str | int | bool] = {}
        for name in self._settings():
            match getattr(self, name):
                case None:
                    continue
                case StrEnum() as value:
                    params[_HARDWARE_KEYS.get(name, name)] = value.value
                case float() as value:
                    params[_HARDWARE_KEYS.get(name, name)] = f"{value:g}"
                case value:
                    params[_HARDWARE_KEYS.get(name, name)] = value
        return params | self.options


class _DeviceBase(Model):
    type: GuestType
    id: VmId
    name: Hostname
    pool: Name
    template: Name | None = Field(default=None, description="Clone this template; or set iso")
    iso: IsoSpec | None = Field(
        default=None,
        description="Create an empty qemu guest that boots this image; or set template",
    )
    cores: PositiveInt
    memory_mb: PositiveInt
    bridges: tuple[Name, ...] = Field(
        min_length=1,
        max_length=2,
        description="First bridge is the primary network",
    )
    role: Role | None = Field(
        default=None,
        description="gateway: other guests on this guest's second bridge are reached through it",
    )
    tags: tuple[Tag, ...] = Field(
        default=(), description="Tags of the guest; the template's own tags are dropped"
    )
    hardware: Hardware | None = Field(default=None, description="QEMU only")
    ansible_user: Name
    ssh_port: Port = 22
    proxy_jumps: tuple[ProxyJump, ...] = Field(
        default=(), description="Extra SSH hops after the Proxmox node, replacing the gateway hop"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if len(set(self.bridges)) != len(self.bridges):
            raise ValueError("bridges: duplicate bridges are not supported")
        if self.role is Role.GATEWAY and len(self.bridges) != 2:
            raise ValueError("role: a gateway needs exactly two bridges (uplink, served)")
        if (self.template is None) == (self.iso is None):
            raise ValueError("set exactly one of template and iso")
        if self.iso is not None and self.type is not GuestType.QEMU:
            raise ValueError("iso: only supported for qemu devices")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags: duplicate tags")
        if self.hardware is not None:
            if self.type is not GuestType.QEMU:
                raise ValueError("hardware: only supported for qemu devices")
            for index in range(len(self.bridges)):
                if f"net{index}" in self.hardware.options:
                    raise ValueError(f"hardware.options.net{index}: set from bridges")
        return self

    @property
    def bridge(self) -> str:
        return self.bridges[0]

    @property
    def served_bridge(self) -> str | None:
        """The bridge this guest is the gateway for, if it is one."""
        return self.bridges[1] if self.role is Role.GATEWAY else None


class DeviceSpec(_DeviceBase):
    """A device as written in a devices file, before config defaults are applied."""

    builder_node: Name | None = None
    host_node: Name | None = None
    migration_storage: Name | None = None
    additional_disk: DiskSpec | None = None

    @model_validator(mode="after")
    def _check_disk(self) -> Self:
        if self.additional_disk is not None and self.type is not GuestType.QEMU:
            raise ValueError("additional_disk: only supported for qemu devices")
        if self.iso is not None:
            if self.builder_node is not None:
                raise ValueError("builder_node: not used with iso; set host_node")
            if self.additional_disk is not None and self.additional_disk.slot == "ide2":
                raise ValueError("additional_disk.slot: ide2 holds the iso")
        taken = {self.additional_disk.slot: "additional_disk"} if self.additional_disk else {}
        if self.iso is not None:
            taken |= {self.iso.disk.slot: "iso.disk"}
            taken |= dict.fromkeys(("ide2", "efidisk0", "tpmstate0"), "iso")
        for key, owner in taken.items():
            if self.hardware is not None and key in self.hardware.options:
                raise ValueError(f"hardware.options.{key}: set from {owner}")
        return self


class DevicesFile(Model):
    devices: list[DeviceSpec] = Field(min_length=1)


class Device(_DeviceBase):
    """A device with every default resolved against the config."""

    template_id: PositiveInt | None = None
    iso: Iso | None = None
    builder_node: Name
    host_node: Name
    migration_storage: Name | None = None
    additional_disk: Disk | None = None
