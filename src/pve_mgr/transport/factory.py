from pve_mgr.errors import ConfigError
from pve_mgr.models import Config, Secrets, TransportKind
from pve_mgr.transport.api import ApiTransport
from pve_mgr.transport.base import Transport
from pve_mgr.transport.dry_run import DryRunTransport
from pve_mgr.transport.ssh import SshTransport, control_dir


def build_transport(config: Config, secrets: Secrets | None = None) -> Transport:
    """The transport the config asks for, talking to the entry node."""
    node = config.nodes[config.entry]
    match config.transport:
        case TransportKind.SSH:
            assert node.ssh is not None
            return SshTransport(node.ssh, sudo=config.use_sudo, control_dir=control_dir())
        case TransportKind.API:
            assert node.api is not None
            secrets = secrets or Secrets()
            if secrets.api_token_id is None or secrets.api_token_secret is None:
                raise ConfigError(
                    "transport is 'api' but PVE_MGR_API_TOKEN_ID and "
                    "PVE_MGR_API_TOKEN_SECRET are not both set (environment or .env)"
                )
            return ApiTransport(
                str(node.api),
                secrets.api_token_id,
                secrets.api_token_secret.get_secret_value(),
                verify_tls=config.verify_tls,
            )


def build_dry_run(config: Config, secrets: Secrets | None = None) -> DryRunTransport:
    return DryRunTransport(build_transport(config, secrets))
