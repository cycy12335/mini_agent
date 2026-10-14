"""Path checks for ordinary workflows, not an OS-level security sandbox."""

from pathlib import Path, PureWindowsPath
import re
import stat

from .models import ToolError


TEXT_SUFFIXES = frozenset({".py", ".pyi", ".md", ".rst", ".txt", ".toml",
                           ".json", ".yaml", ".yml", ".ini", ".cfg"})
BLOCKED_DIRECTORIES = frozenset({"node_modules", "venv", "env", "__pycache__",
                                  "build", "dist", "site-packages"})
SENSITIVE_NAME = re.compile(
    r"credential|secret|password|(?:^|[_.-])(?:tokens?|api[_-]?keys?|private[_-]?keys?|"
    r"service[_-]?accounts?|id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:$|[_.-])",
    re.IGNORECASE,
)
WINDOWS_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def _check_links(path: Path) -> None:
    # lstat is available on 3.11; is_junction was only added in 3.12.
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ToolError("LINK_FORBIDDEN", "Symbolic links and reparse points are forbidden")


class PolicyGuard:
    def resolve_edit(self, relative: str) -> Path:
        path = self.resolve(relative)
        if path.suffix.lower() not in {".py", ".pyi"} or path.name.lower() in {"setup.py", "conftest.py"}:
            raise ToolError("EDIT_FORBIDDEN", "Only Python source/tests may be edited; setup and test configuration are excluded")
        return path

    def __init__(self, project: Path):
        # Do not resolve away symlink evidence before checking the supplied root.
        supplied = Path(project).absolute()
        _check_links(supplied)
        self.root = supplied.resolve(strict=True)
        if not self.root.is_dir():
            raise ToolError("NOT_DIRECTORY", "Project must be an existing directory")
        if any(p.lower() in {".git", ".venv", ".mini-agent", ".aws", ".ssh"}
               or p.lower() in BLOCKED_DIRECTORIES for p in self.root.parts):
            raise ToolError("PROTECTED_PROJECT", "Project is inside an excluded directory")
        agent_code = Path(__file__).resolve().parent
        # Wheel code may be outside the checkout. Protect recognizable Agent source too,
        # while allowing examples/subprojects beneath a checkout as independent targets.
        candidates = [self.root / "src" / "mini_agent", self.root / "mini_agent"]
        candidates.extend(p for p in (self.root, *self.root.parents) if p.name.lower() == "mini_agent")
        source_checkout = any(all((p / name).is_file() for name in ("__init__.py", "cli.py", "policy.py"))
                              for p in candidates)
        if self.root.is_relative_to(agent_code) or agent_code.is_relative_to(self.root) or source_checkout:
            raise ToolError("PROTECTED_PROJECT", "Choose a target separate from Agent source")

    def resolve(self, relative: str, *, directory: bool = False) -> Path:
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise ToolError("INVALID_PATH", "Path must be a nonempty relative string (max 1024 chars)")
        if relative == "." and directory:
            _check_links(self.root)
            return self.root
        windows = PureWindowsPath(relative)
        if windows.drive or windows.root or relative.startswith(("/", "\\")):
            raise ToolError("INVALID_PATH", "Absolute, drive-relative and UNC paths are forbidden")
        parts = relative.replace("\\", "/").split("/")
        for part in parts:
            if (part in {"", ".", ".."} or part.endswith((".", " "))
                    or any(ord(c) < 32 or c in ':*?"<>|' for c in part)
                    or WINDOWS_DEVICE.match(part)):
                raise ToolError("INVALID_PATH", "Unsafe path component")
            if (part.startswith(".") or part.lower() in BLOCKED_DIRECTORIES
                    or SENSITIVE_NAME.search(part)):
                raise ToolError("FILTERED_PATH", "Hidden, generated or sensitive paths are excluded")
        path = self.root.joinpath(*parts)
        _check_links(path)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ToolError("OUTSIDE_PROJECT", "Path escapes the project")
        info = resolved.stat()
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ToolError("NOT_DIRECTORY", "Expected a directory")
        else:
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("NOT_FILE", "Only regular files are readable")
            if info.st_nlink > 1:
                raise ToolError("LINK_FORBIDDEN", "Hard-linked files are excluded")
            if resolved.suffix.lower() not in TEXT_SUFFIXES:
                raise ToolError("UNSUPPORTED_FILE", "File type is not in the text allowlist")
        return resolved
