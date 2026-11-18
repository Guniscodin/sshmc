import asyncio
import struct
import logging

logger = logging.getLogger(__name__)

class RCONError(Exception):
    """Base exception for RCON errors.

    command_sent is True when the request packet was flushed to the socket
    before the failure (e.g. the response timed out or the connection dropped).
    Callers use it to decide whether the command still reached the server.
    """
    def __init__(self, message: str = "", *, command_sent: bool = False):
        super().__init__(message)
        self.command_sent = command_sent

class RCONAuthError(RCONError):
    """Raised when authentication fails."""
    pass

# RCON Packet Types
SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2

def _pack_packet(req_id: int, req_type: int, payload: str) -> bytes:
    """Packs an RCON packet into its binary format."""
    payload_bytes = payload.encode("utf-8")
    # Packet format: Length(int32) + ID(int32) + Type(int32) + Payload + Null(2 bytes)
    packet = struct.pack("<ii", req_id, req_type) + payload_bytes + b"\x00\x00"
    # Prepend the total length of the packet
    return struct.pack("<i", len(packet)) + packet

async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    """Reads and unpacks a single RCON response packet."""
    try:
        # Read the length of the incoming packet (first 4 bytes)
        length_data = await reader.readexactly(4)
        length = struct.unpack("<i", length_data)[0]
        if length < 10 or length > 4096:
            raise RCONError(f"Invalid RCON packet length: {length}")
        
        # Read the rest of the packet based on the length
        data = await reader.readexactly(length)
        
        # Unpack ID, Type, and Payload (payload ends with two null bytes)
        req_id, req_type = struct.unpack("<ii", data[:8])
        if data[-2:] != b"\x00\x00":
            raise RCONError("Invalid RCON packet terminator")
        payload = data[8:-2].decode("utf-8")
        
        return req_id, req_type, payload
    except (asyncio.IncompleteReadError, UnicodeDecodeError, struct.error) as e:
        raise RCONError("Connection closed unexpectedly while reading packet") from e

async def send_command(
    host: str, port: int, password: str, command: str, timeout: float = 5.0
) -> str:
    """
    Connects to an RCON server, authenticates, sends a command, and returns the response.
    Strictly typed, heavily logged, and async.
    """
    logger.debug(f"Connecting to RCON at {host}:{port}")
    
    try:
        # Open async connection with a strict timeout
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as e:
        logger.error(f"RCON connection failed to {host}:{port}: {e}")
        raise RCONError(f"Could not connect to RCON: {e}") from e

    try:
        # 1. Authentication Phase
        auth_packet = _pack_packet(req_id=1, req_type=SERVERDATA_AUTH, payload=password)
        writer.write(auth_packet)
        await writer.drain()

        # Read auth response. The Source RCON spec permits an empty
        # RESPONSE_VALUE packet before the auth response, so keep reading
        # until the auth response itself arrives (max 3 packets).
        auth_packet = None
        for _ in range(3):
            candidate = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
            cand_id, cand_type, _ = candidate
            if cand_id == -1 or cand_type != 0:
                auth_packet = candidate
                break
        if auth_packet is None:
            raise RCONError("Unexpected RCON authentication response.")
        auth_id, auth_type, _ = auth_packet

        # According to RCON spec, failed auth returns ID -1
        if auth_id == -1:
            logger.error("RCON authentication failed: Bad password.")
            raise RCONAuthError("Authentication failed: incorrect RCON password.")
        if auth_id != 1 or auth_type != SERVERDATA_EXECCOMMAND:
            raise RCONError("Unexpected RCON authentication response.")

        logger.debug("RCON authentication successful.")

        # 2. Command Phase
        cmd_packet = _pack_packet(req_id=2, req_type=SERVERDATA_EXECCOMMAND, payload=command)
        try:
            writer.write(cmd_packet)
            await writer.drain()
        except Exception as e:
            # The packet never left the socket, so the server never got it.
            raise RCONError(f"RCON write failed while sending '{command}': {e}", command_sent=False) from e

        # Read command response
        try:
            response_id, _, response = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
        except asyncio.TimeoutError as e:
            logger.error(f"RCON timeout while executing '{command}'.")
            raise RCONError("RCON operation timed out.", command_sent=True) from e
        except RCONError as e:
            # Malformed/dropped response packet — the command may well have run.
            raise RCONError(str(e), command_sent=True) from e
        if response_id != 2:
            raise RCONError("Unexpected RCON command response.", command_sent=True)
        
        logger.debug(f"RCON command '{command}' executed successfully.")
        return response

    except asyncio.TimeoutError as e:
        logger.error(f"RCON timeout while executing '{command}'.")
        raise RCONError("RCON operation timed out.") from e
    finally:
        # Always close the connection cleanly. wait_closed() can surface a reset
        # raised inside the protocol callback, which is outside the OSError path —
        # cleanup must never raise out of the finally.
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.debug("RCON connection closed with a socket error.")
