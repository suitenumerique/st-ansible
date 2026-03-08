"""Molecule Lima Driver Module.

A Molecule driver for Lima VM, enabling testing of Ansible roles on Linux
with native virtualization support.
"""

from __future__ import annotations

from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Any

from molecule import logger, util
from molecule.api import Driver
from molecule.status import Status

if TYPE_CHECKING:
    from molecule.config import Config

LOG = logger.get_logger(__name__)


class Lima(Driver):
    """Molecule Driver for Lima VM.

    This driver enables testing Ansible roles using Lima virtual machines
    on Linux. It provides instance lifecycle management through Lima.
    """

    default_name = "molecule-lima"

    def __init__(self, config: Config | None = None) -> None:
        """Initialize the Lima driver.

        Args:
            config: An instance of a Molecule config.
        """
        super().__init__(config)  # type: ignore[arg-type]
        self._name = self.default_name

    @property
    def name(self) -> str:
        """Return driver name."""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        """Set driver name."""
        self._name = value

    @property
    def delegated(self) -> bool:
        """Return whether this is a delegated driver."""
        return False

    @property
    def managed(self) -> bool:
        """Return whether instances are managed by Molecule."""
        return True

    @property
    def login_cmd_template(self) -> str:
        """Return login command template."""
        return "limactl shell {instance}"

    @property
    def default_ssh_connection_options(self) -> list[str]:
        """Return default SSH connection options."""
        return [
            "-o UserKnownHostsFile=/dev/null",
            "-o StrictHostKeyChecking=no",
            "-o IdentitiesOnly=yes",
        ]

    @property
    def default_safe_files(self) -> list[str]:
        """Return default safe files."""
        return [self.instance_config]

    @property
    def testinfra_options(self) -> dict[str, str]:
        """Return testinfra specific options."""
        if not self._config.provisioner:
            return {}
        return {
            "connection": "ansible",
            "ansible-inventory": self._config.provisioner.inventory_file,
        }

    @property
    def required_collections(self) -> dict[str, str]:
        """Return required Ansible collections."""
        return {
            "community.general": "9.0.0",
        }

    def login_options(self, instance_name: str) -> dict[str, str]:
        """Return login options for instance."""
        d = {"instance": instance_name}
        try:
            return util.merge_dicts(d, self._get_instance_config(instance_name))
        except (StopIteration, OSError):
            return d

    def ansible_connection_options(self, instance_name: str) -> dict[str, Any]:
        """Return Ansible connection options for instance."""
        try:
            d = self._get_instance_config(instance_name)
            return {
                "ansible_user": d["user"],
                "ansible_host": d["address"],
                "ansible_port": d["port"],
                "ansible_ssh_private_key_file": d["identity_file"],
                "ansible_connection": "ssh",
                "ansible_ssh_common_args": " ".join(
                    self.default_ssh_connection_options
                ),
            }
        except StopIteration:
            return {}
        except OSError:
            return {}

    def _get_instance_config(self, instance_name: str) -> dict[str, Any]:
        """Get instance configuration."""
        instance_config_dict = util.safe_load_file(self._config.driver.instance_config)
        return next(
            item for item in instance_config_dict if item["instance"] == instance_name
        )

    def status(self) -> list[Status]:
        """Collect the instances state and returns a list."""
        status_list: list[Status] = []
        instances = self._config.platforms.instances

        if not instances:
            instances = [{"name": ""}]

        for platform in instances:
            instance_name = platform.get("name", "")
            status_list.append(
                Status(
                    instance_name=instance_name,
                    driver_name=self.name,
                    provisioner_name=self._config.provisioner.name if self._config.provisioner else "",
                    scenario_name=self._config.scenario.name,
                    created=str(self._config.state.created).lower(),
                    converged=str(self._config.state.converged).lower(),
                ),
            )

        return status_list

    def schema_file(self) -> str | None:
        """Return schema file path."""
        schema_path = Path(self._path) / "schema.json"
        if schema_path.is_file():
            return str(schema_path)
        return None

    def sanity_checks(self) -> None:
        """Perform sanity checks."""
        if not which("limactl"):
            util.sysexit_with_message("limactl executable was not found!")
