"""V380 SD-card fetcher — download recordings from a V380 / V380 Pro camera's SD card
over the vendor cloud, with no phone and no card removal.

Protocol knowledge derived from the clean-room reverse engineering in
cimplesid/v380-mac (OpenV380, MIT) and cross-checked byte-for-byte against
captured app traffic.

MIT licensed. Use only on cameras you own.
"""

from .client import V380Camera, V380Error, Recording  # noqa: F401

__version__ = "0.1.0"
