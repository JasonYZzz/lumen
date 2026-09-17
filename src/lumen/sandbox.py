"""Fail-closed OS sandbox preparation for model-triggered subprocesses."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lumen.config import SandboxConfig


class SandboxUnavailableError(RuntimeError):
    code = "sandbox_unavailable"


@dataclass(frozen=True, slots=True)
class PreparedSandboxCommand:
    argv: list[str]
    env: dict[str, str]
    temp_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


class SandboxRunner:
    """Build Seatbelt/bubblewrap commands without granting model escalation."""

    def __init__(self, workspace: str | Path, config: SandboxConfig) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config

    def prepare(
        self,
        argv: list[str],
        *,
        cwd: str | Path,
        overrides: dict[str, str] | None = None,
        read_paths: tuple[Path, ...] = (),
        writable_protected_paths: tuple[Path, ...] = (),
        workspace_writable: bool = True,
    ) -> PreparedSandboxCommand:
        if not argv:
            raise ValueError("argv must not be empty")
        temp_root = Path(tempfile.mkdtemp(prefix="lumen-sandbox-")).resolve()
        home = temp_root / "home"
        tmp = temp_root / "tmp"
        home.mkdir(mode=0o700)
        tmp.mkdir(mode=0o700)
        environment = {
            name: value
            for name in self.config.env_allow
            if (value := os.environ.get(name)) is not None
        }
        environment.update(overrides or {})
        environment["HOME"] = str(home)
        environment["TMPDIR"] = str(tmp)
        environment["LUMEN_WORKSPACE"] = str(self.workspace)
        try:
            executable = self._resolve_executable(argv[0], environment)
            command = [str(executable), *argv[1:]]
            if self.config.mode == "disabled":
                return PreparedSandboxCommand(command, environment, temp_root)
            system = platform.system()
            if system == "Darwin":
                wrapped = self._seatbelt(
                    command,
                    Path(cwd).resolve(),
                    temp_root,
                    read_paths,
                    writable_protected_paths=writable_protected_paths,
                    workspace_writable=workspace_writable,
                )
            elif system == "Linux":
                wrapped = self._bubblewrap(
                    command,
                    Path(cwd).resolve(),
                    temp_root,
                    read_paths,
                    writable_protected_paths=writable_protected_paths,
                    workspace_writable=workspace_writable,
                )
            else:
                raise SandboxUnavailableError(
                    f"sandbox_unavailable: no sandbox adapter for {system or 'unknown platform'}"
                )
        except BaseException:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        return PreparedSandboxCommand(wrapped, environment, temp_root)

    @staticmethod
    def _resolve_executable(value: str, env: dict[str, str]) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            if not candidate.is_file():
                raise FileNotFoundError(f"executable not found: {value}")
            return candidate.resolve()
        resolved = shutil.which(value, path=env.get("PATH"))
        if resolved is None:
            raise FileNotFoundError(f"executable not found on sanitized PATH: {value}")
        return Path(resolved).resolve()

    def _read_roots(self, command: list[str], read_paths: tuple[Path, ...]) -> list[Path]:
        candidates = [
            self.workspace,
            Path(command[0]).resolve(),
            Path(sys.executable).resolve(),
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            *(Path(item).expanduser().resolve() for item in self.config.extra_read_paths),
            *(item.resolve() for item in read_paths),
        ]
        for value in ("/usr", "/bin", "/lib", "/lib64", "/System", "/Library"):
            path = Path(value)
            if path.exists():
                candidates.append(path.resolve())
        # Homebrew executables can dynamically link libraries from another
        # formula. Grant only installed library directories, not the prefix's
        # configuration, private data, or arbitrary user files.
        if platform.system() == "Darwin":
            for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
                for library in (prefix / "opt").glob("*/lib"):
                    # dyld traverses the opt symlink before opening the real
                    # library. Seatbelt also needs that spelling's ancestors.
                    candidates.extend((library, library.resolve()))
        # Public runtime configuration, not all of /etc (which can hold secrets).
        # TLS clients read openssl.cnf even for offline commands such as curl -V.
        for value in (
            "/etc/ssl/openssl.cnf", "/etc/ssl/cert.pem", "/etc/ssl/certs",
            "/etc/pki/tls/openssl.cnf", "/etc/pki/tls/certs",
            "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
        ):
            # Keep the public spelling too: bubblewrap must mount e.g.
            # /etc/resolv.conf even when it points into /run on the host.
            candidates.extend((Path(value), Path(value).resolve()))
        return _dedupe_existing(candidates)

    def _write_roots(self, temp_root: Path, *, workspace_writable: bool) -> list[Path]:
        return _dedupe_existing(
            [
                temp_root,
                *(
                    [
                        self.workspace,
                        *(Path(item).expanduser().resolve() for item in self.config.extra_write_paths),
                    ]
                    if workspace_writable
                    else []
                ),
            ]
        )

    def _seatbelt(
        self,
        command: list[str],
        cwd: Path,
        temp_root: Path,
        read_paths: tuple[Path, ...],
        *,
        writable_protected_paths: tuple[Path, ...],
        workspace_writable: bool,
    ) -> list[str]:
        adapter = Path("/usr/bin/sandbox-exec")
        if not adapter.is_file():
            raise SandboxUnavailableError("sandbox_unavailable: /usr/bin/sandbox-exec is missing")
        clauses = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            # dyld/getcwd need metadata access to the filesystem root itself;
            # ``subpath`` rules below do not include their ancestor entry.
            '(allow file-read* (literal "/"))',
        ]
        write_roots = self._write_roots(temp_root, workspace_writable=workspace_writable)
        read_roots = _dedupe_existing([*self._read_roots(command, read_paths), *write_roots])
        clauses.extend(
            f'(allow file-read* (subpath "{_escape(path)}"))'
            for path in read_roots
        )
        # getcwd/dyld and xcode-select traverse ancestors and the developer-dir
        # symlink. Metadata permits traversal, not listing or reading their data.
        ancestors = {parent for path in [*read_roots, cwd] for parent in path.parents}
        ancestors.update({Path("/var"), Path("/var/select"), Path("/private/var/select")})
        ancestors.update({Path("/var/select/developer_dir"), Path("/private/var/select/developer_dir")})
        clauses.extend(
            f'(allow file-read-metadata (literal "{_escape(path)}"))'
            for path in sorted(ancestors)
        )
        clauses.extend(
            f'(allow file-read* (literal "{path}"))'
            for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
        )
        clauses.append('(allow file-write* (literal "/dev/null"))')
        clauses.extend(
            f'(allow file-write* (subpath "{_escape(path)}"))'
            for path in write_roots
        )
        writable_protected = {Path(os.path.abspath(path)) for path in writable_protected_paths}
        for protected in (self.workspace / ".git", self.workspace / ".lumen"):
            if protected in writable_protected:
                clauses.append(f'(allow file-write* (subpath "{_escape(protected)}"))')
                continue
            # ``literal`` blocks creation of the root itself; ``subpath``
            # protects descendants when it already exists. Keep both rules
            # unconditional so an empty workspace cannot create control
            # state during the command.
            clauses.append(f'(deny file-write* (literal "{_escape(protected)}"))')
            clauses.append(f'(deny file-write* (subpath "{_escape(protected)}"))')
        if self.config.network:
            clauses.append("(allow network*)")
        clauses.append(f'(allow file-read* (subpath "{_escape(cwd)}"))')
        return [str(adapter), "-p", "\n".join(clauses), *command]

    def _bubblewrap(
        self,
        command: list[str],
        cwd: Path,
        temp_root: Path,
        read_paths: tuple[Path, ...],
        *,
        writable_protected_paths: tuple[Path, ...],
        workspace_writable: bool,
    ) -> list[str]:
        adapter_value = shutil.which("bwrap")
        if adapter_value is None:
            raise SandboxUnavailableError("sandbox_unavailable: bubblewrap (bwrap) is missing")
        args = [adapter_value, "--die-with-parent", "--new-session", "--unshare-all"]
        if self.config.network:
            args.append("--share-net")
        args.extend(["--proc", "/proc", "--dev", "/dev"])
        read_roots = self._read_roots(command, read_paths)
        for path in read_roots:
            args.extend(["--ro-bind", str(path), str(path)])
        for path in self._write_roots(temp_root, workspace_writable=workspace_writable):
            args.extend(["--bind", str(path), str(path)])
        writable_protected = {Path(os.path.abspath(path)) for path in writable_protected_paths}
        for protected in (self.workspace / ".git", self.workspace / ".lumen"):
            if protected not in writable_protected and protected.exists():
                args.extend(["--ro-bind", str(protected), str(protected)])
        args.extend(["--chdir", str(cwd), "--", *command])
        return args


def _dedupe_existing(paths: list[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        if path.exists():
            unique[str(path)] = path
    return sorted(unique.values(), key=lambda item: (len(item.parts), str(item)))


def _escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["PreparedSandboxCommand", "SandboxRunner", "SandboxUnavailableError"]
