"""Prevent two coordinators from owning the same local data directory."""
import os


class InstanceAlreadyRunning(RuntimeError):
    """A running process already holds the data-directory lock."""


class InstanceLock:
    def __init__(self, root):
        self.path = root / "instance.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            raise InstanceAlreadyRunning("Another CDS server owns this data directory. Use one API instance or a different CDS_DATA_DIR.") from error

    def release(self):
        if self.file:
            self.file.close()
            self.file = None
