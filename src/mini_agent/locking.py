"""OS-owned leases: process death releases ownership without deleting lock files."""
import os
from pathlib import Path
import time

from .models import ToolError
from .policy import _check_links


class FileLease:
    def __init__(self, path, *, cancelled=lambda: False, timeout=0):
        self.path = Path(path)
        self.cancelled, self.timeout = cancelled, timeout
        self.stream = None

    def __enter__(self):
        _check_links(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            _check_links(self.path)
            if self.path.stat().st_nlink != 1:
                raise ValueError("Hard-linked lock is forbidden")
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if self.cancelled():
                    raise ToolError("CANCELLED", "Cancelled while waiting for repository ownership")
                try:
                    self.stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ToolError("REPOSITORY_BUSY", "Repository or run is owned by another task") from exc
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        except BaseException:
            self.stream.close()
            self.stream = None
            raise

    def __exit__(self, *args):
        if self.stream:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


class RepositoryLease(FileLease):
    def __init__(self, project, **kwargs):
        directory = Path(project) / ".mini-agent"
        _check_links(directory.parent)
        if directory.exists() or directory.is_symlink():
            _check_links(directory)
        directory.mkdir(exist_ok=True)
        super().__init__(directory / "core.lock", **kwargs)
