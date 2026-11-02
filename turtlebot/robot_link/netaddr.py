"""The IPv4 address of a network interface (Linux). The Dell's address on the Wi-Fi is given by DHCP and can change, so the
API looks it up when it starts instead of having it written in a file."""

import fcntl
import socket
import struct
from typing import Optional

SIOCGIFADDR = 0x8915


def iface_ipv4(name: str) -> Optional[str]:
    """The IPv4 address of interface `name`, or None if there is no such interface or it has no address."""
    if not name or len(name) > 15:
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()))
        except OSError:
            return None
    return socket.inet_ntoa(packed[20:24])
