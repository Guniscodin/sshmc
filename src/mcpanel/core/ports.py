import socket
import logging

logger = logging.getLogger(__name__)

def is_port_available(port: int) -> bool:
    """
    Checks if a TCP port is available to bind on all interfaces (0.0.0.0).
    Returns True if available, False if in use.
    """
    # Create a TCP socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Set SO_REUSEADDR to avoid "Address already in use" errors during rapid restarts
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            # Try to bind to the port on all interfaces
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            logger.warning(f"Port {port} is currently in use.")
            return False

def find_available_port(start_port: int, reserved_ports: set[int]) -> int:
    """Find a currently free TCP port that is not already assigned to a profile."""
    for port in range(start_port, 65536):
        if port not in reserved_ports and is_port_available(port):
            return port
    raise RuntimeError(f"No free TCP port found at or above {start_port}.")
