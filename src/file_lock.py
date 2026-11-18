"""Small flock compatibility layer for the existing task/workflow locks."""
import os

if os.name != "nt":
    from fcntl import flock, LOCK_EX, LOCK_NB, LOCK_UN
else:
    import msvcrt
    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(fd, operation):
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        mode = (msvcrt.LK_UNLCK if operation & LOCK_UN else
                msvcrt.LK_NBLCK if operation & LOCK_NB else msvcrt.LK_LOCK)
        try:
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
