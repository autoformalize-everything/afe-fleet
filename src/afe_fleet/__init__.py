"""afe-fleet: distributed, cooperatively coordinated agent workers.

See docs/DESIGN.md. Entry point: `afe-fleet` (afe_fleet.cli:main).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("afe-fleet")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+source"
