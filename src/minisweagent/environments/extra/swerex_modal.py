"""Modal environment using SWE-ReX for remote execution."""

import asyncio
import os
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    import modal

    modal.enable_output()  # Enable Modal output to see image build logs
except ImportError:
    pass

from swerex.deployment.modal import ModalDeployment
from swerex.runtime.abstract import Command as RexCommand
from minisweagent import logger


@dataclass
class SwerexModalEnvironmentConfig:
    image: str
    cwd: str = "/"
    """Working directory in which to execute commands."""
    env: dict[str, str] = field(default_factory=dict)
    """Environment variables to set in the container."""
    forward_env: list[str] = field(default_factory=list)
    """Environment variables to forward to the container."""
    timeout: int = 30
    """Timeout for executing commands in the container."""
    install_pipx: bool = True
    """Whether to install pipx in the modal container."""
    startup_timeout: int = 900
    """Timeout for starting the modal deployment (in seconds)."""
    deployment_extra_kwargs: dict[str, Any] = field(default_factory=dict)
    """Extra kwargs to pass to ModalDeployment."""


class SwerexModalEnvironment:
    def __init__(self, **kwargs):
        """This class executes bash commands in a Modal container using SWE-ReX for sandboxing."""
        self.config = SwerexModalEnvironmentConfig(**kwargs)

        # Extract startup_timeout from config
        startup_timeout = self.config.startup_timeout

        # Prepare modal_sandbox_kwargs
        modal_sandbox_kwargs = dict(
            self.config.deployment_extra_kwargs.get("modal_sandbox_kwargs", {})
        )

        deployment_kwargs = {
            "image": self.config.image,
            "install_pipx": self.config.install_pipx,
            "startup_timeout": startup_timeout,
            "modal_sandbox_kwargs": modal_sandbox_kwargs,
        }

        # Add any other deployment_extra_kwargs except modal_sandbox_kwargs (already handled)
        for key, value in self.config.deployment_extra_kwargs.items():
            if key != "modal_sandbox_kwargs":
                deployment_kwargs[key] = value

        logger.info(f"Deployment kwargs: {deployment_kwargs}")
        self.deployment = ModalDeployment(**deployment_kwargs)
        asyncio.run(self.deployment.start())

        # Set environment variables after the container is running
        # Modal Sandbox.create() does not support passing environment variables at creation time
        # Instead, we set them by executing bash export commands after the container starts (like SWE-agent does)
        self._set_environment_variables()

        # Determine working directory by executing pwd in the container
        self.working_dir = self._get_working_dir()

    def _set_environment_variables(self):
        """Set environment variables in the running container by executing bash commands."""
        # Collect all environment variables to set
        env_vars = dict(self.config.env)

        # Forward environment variables from the host if specified
        for env_var in self.config.forward_env:
            if env_var in os.environ:
                env_vars[env_var] = os.environ[env_var]

        if not env_vars:
            return

        # Build export commands
        env_setters = [f"export {k}={shlex.quote(str(v))}" for k, v in env_vars.items()]
        command = " && ".join(env_setters)

        # Execute the export commands in the container
        result = self.execute(command)
        if result["returncode"] != 0:
            raise RuntimeError(
                f"Failed to set environment variables: {result['output']}"
            )

    def _get_working_dir(self) -> str:
        """Determine the current working directory in the container."""
        result = self.execute("pwd")
        if result["returncode"] != 0:
            logger.warning(
                f"Failed to determine working directory, using cwd: {self.config.cwd}"
            )
            return self.config.cwd
        return result["output"].strip()

    def execute(
        self, command: str, cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        """Execute a command in the environment and return the raw output."""
        output = asyncio.run(
            self.deployment.runtime.execute(
                RexCommand(
                    command=command,
                    shell=True,
                    check=False,
                    cwd=cwd or self.config.cwd,
                    timeout=timeout or self.config.timeout,
                    merge_output_streams=True,
                )
            )
        )
        return {
            "output": output.stdout,
            "returncode": output.exit_code,
        }

    def get_template_vars(self) -> dict[str, Any]:
        return asdict(self.config) | {"working_dir": self.working_dir}

    def __del__(self):
        """Clean up the deployment when the environment is destroyed."""
        try:
            if hasattr(self, "deployment") and self.deployment:
                asyncio.run(self.deployment.stop())
        except Exception:
            pass  # Ignore errors during cleanup
