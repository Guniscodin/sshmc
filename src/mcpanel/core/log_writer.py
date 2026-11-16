"""Small detached stdout relay that rotates Minecraft logs without rename races."""
import os
import subprocess
import sys
from pathlib import Path


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _rotate(path: Path, root: Path, keep: int) -> None:
    _inside(root, path)
    oldest = path.with_name(path.name + f".{keep}")
    try:
        _inside(root, oldest).unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    for index in range(keep - 1, 0, -1):
        source = path.with_name(path.name + f".{index}")
        destination = path.with_name(path.name + f".{index + 1}")
        if source.exists():
            os.replace(_inside(root, source), _inside(root, destination))
    if path.exists():
        os.replace(_inside(root, path), _inside(root, path.with_name(path.name + ".1")))


def main() -> int:
    if len(sys.argv) < 7 or sys.argv[5] != "--":
        return 2
    log_path = Path(sys.argv[1])
    data_root = Path(sys.argv[2])
    max_bytes = max(1, int(sys.argv[3]))
    keep = max(1, int(sys.argv[4]))
    command = sys.argv[6:]
    if not command:
        return 2
    _inside(data_root, log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size >= max_bytes:
        _rotate(log_path, data_root, keep)

    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except OSError as exc:
        with open(log_path, "ab") as output:
            output.write(f"SSHMC could not launch Minecraft: {exc}\n".encode())
        return 127

    current_size = log_path.stat().st_size if log_path.exists() else 0
    output = open(log_path, "ab", buffering=0)
    try:
        while True:
            chunk = os.read(child.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                if current_size >= max_bytes:
                    output.close()
                    _rotate(log_path, data_root, keep)
                    output = open(log_path, "ab", buffering=0)
                    current_size = 0
                length = min(len(chunk) - offset, max_bytes - current_size)
                output.write(chunk[offset:offset + length])
                current_size += length
                offset += length
    finally:
        output.close()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
