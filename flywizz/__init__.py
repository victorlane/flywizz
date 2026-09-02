"""
Flywizz - An open-source unofficial API wrapper to get flight data from Wizz Air.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .async_client import AsyncWizzAir
from .misc import (
    AvailabilitySearch,
    Connection,
    Country,
    Culture,
    Departure,
    DestinationFare,
    FareChartEntry,
    FareChartSearch,
    FlightDate,
    FlightStatus,
    Network,
    Price,
    Station,
    TimetableEntry,
    TimetableSearch,
)
from .transport import (
    AsyncCachingTransport,
    AsyncTransport,
    AsyncWizzairTransport,
    BotGateError,
    CachingTransport,
    Transport,
    ValidationError,
    WizzairException,
    WizzairTransport,
)
from .wizzair import WizzAir

try:
    __version__ = _pkg_version("Flywizz")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "WizzAir",
    "AsyncWizzAir",
    "WizzairException",
    "BotGateError",
    "ValidationError",
    "WizzairTransport",
    "AsyncWizzairTransport",
    "CachingTransport",
    "AsyncCachingTransport",
    "Transport",
    "AsyncTransport",
    "AvailabilitySearch",
    "Connection",
    "Country",
    "Culture",
    "Departure",
    "FlightDate",
    "DestinationFare",
    "FareChartEntry",
    "FareChartSearch",
    "FlightStatus",
    "Network",
    "Price",
    "Station",
    "TimetableEntry",
    "TimetableSearch",
]
