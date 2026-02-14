#!/usr/bin/env python3
## ○════════════════════════════════════════════════════════════════════════○ ##
## ○════════════════════════════════════════════════════════════════════════○ ##
## ○═════  ██████╗██╗  ██╗██╗██████╗  ██████╗██╗  ██╗ █████╗ ████████╗ ═════○ ##
##        ██╔════╝██║  ██║██║██╔══██╗██╔════╝██║  ██║██╔══██╗╚══██╔══╝        ##
##        ██║     ███████║██║██████╔╝██║     ███████║███████║   ██║           ##
##        ██║     ██╔══██║██║██╔═══╝ ██║     ██╔══██║██╔══██║   ██║           ##
##        ╚██████╗██║  ██║██║██║     ╚██████╗██║  ██║██║  ██║   ██║           ##
## ○═════  ╚═════╝╚═╝  ╚═╝╚═╝╚═╝      ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝    ═════○ ##
## ○════════════════════════════════════════════════════════════════════════○ ##
## ○════════════════════════════════════════════════════════════════════════○ ##
##           Copyright © 2026 Tyler J. Kenney. All rights reserved.           ##
## ○════════════════════════════════════════════════════════════════════════○ ##


from __future__ import annotations

DESCRIPTION="""
ChipChat - I/O monitor with meters for disks and network interfaces
"""

import glob
import re
import subprocess
import time
import shutil
import yaml

from argparse import ArgumentParser
from argparse import RawTextHelpFormatter as RTHF
from pathlib import Path
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich.style import Style


# ASCII art logo (3 lines tall)
LOGO_TEXT = [
    "╔═╗┬ ┬┬┌─┐╔═╗┬ ┬┌─┐┌┬┐",
    "║  ├─┤│├─┘║  ├─┤├─┤ │ ",
    "╚═╝┴ ┴┴┴  ╚═╝┴ ┴┴ ┴ ┴ ",
]
LOGO_WIDTH = len(LOGO_TEXT[0])  # 22


def render_logo(width: int) -> list[Text]:
    """Render the logo with scalable data-stream flair on each side.

    Returns a list of 3 Text objects that scale to fill the given width.
    """
    # Fixed elements: "○" + "─...─" + "┤ " + logo + " ├" + "─...─" + "○"
    # Overhead: 2 (endpoints) + 4 (separators) = 6
    overhead = 6
    available = width - LOGO_WIDTH - overhead

    if available < 2:
        # Not enough space, just return plain logo
        return [Text(line, style="bright_black") for line in LOGO_TEXT]

    per_side = available // 2
    dashes = "─" * per_side

    result = []
    for line in LOGO_TEXT:
        t = Text()
        t.append("○", style="bright_black")
        t.append(dashes, style="bright_black")
        t.append("┤ ", style="bright_black")
        t.append(line, style="bright_black")
        t.append(" ├", style="bright_black")
        t.append(dashes, style="bright_black")
        t.append("○", style="bright_black")
        result.append(t)

    return result


@dataclass
class DiskStats:
    """Raw stats from /proc/diskstats"""
    reads_completed: int
    writes_completed: int
    sectors_read: int
    sectors_written: int
    io_ms: int  # milliseconds spent doing I/O
    timestamp: float


@dataclass
class NetStats:
    """Raw stats from /sys/class/net/*/statistics/"""
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    timestamp: float


@dataclass
class DiskMetrics:
    """Computed metrics from stat deltas"""
    util_pct: float
    read_mbps: float
    write_mbps: float
    total_mbps: float
    read_iops: float
    write_iops: float
    total_iops: float


@dataclass
class NetMetrics:
    """Computed metrics from net stat deltas"""
    rx_mbps: float
    tx_mbps: float
    total_mbps: float
    rx_pps: float
    tx_pps: float
    total_pps: float


@dataclass
class WifiInfo:
    """WiFi connection information from iw dev"""
    signal_dbm: float | None
    ssid: str | None
    freq_mhz: float | None


@dataclass
class MeterConfig:
    """Per-meter configuration"""
    type: str  # "utilization", "iops", "bandwidth", "blank"
    label: str  # display label (max 4 chars)
    max_value: float | None  # None = auto, value = explicit (MB/s for bandwidth, count for iops, % for util)
    halflife: float | None  # seconds for exponential decay (None = all-time max, only for auto meters)
    color_in: str  # color for read/rx (or fill color for util)
    color_out: str  # color for write/tx (ignored for util)


@dataclass
class Threshold:
    """A threshold with value and style"""
    value: float | None  # None = no lower bound (always matches if no higher threshold matched)
    style: str  # Rich style string like 'green', 'red blink'


@dataclass
class TextConfig:
    """Per-text-element configuration"""
    type: str  # "name", "temp", "usage", "signal", "ssid", "ip", "freq", "link_speed", "blank"
    thresholds: list[Threshold]  # ordered list of thresholds
    val: str | None  # for name: display value
    downsample: int  # for temp: poll every N refreshes (0 = disable)
    inverted: bool = False  # for signal: lower values are worse (use <= instead of >=)
    style: str | None = None  # for name, ssid, ip, freq: value style
    scale: float = 1.0  # for usage: multiply computed capacity by this
    offset: float = 0.0  # for usage: add this to scaled capacity
    label: str | None = None  # custom label (includes colon), None = use default
    align: str = "right"  # value alignment: "left" or "right"


@dataclass
class DiskConfig:
    """Per-disk configuration"""
    type: str | None  # "hdd", "ssd", or "net"
    meters: list[MeterConfig]
    text: list[TextConfig]
    mount_points: list[str]  # empty to hide capacity
    name: str  # display name
    text_width: int | None  # minimum text column width (None = auto)

    def get_height(self) -> int:
        """Return the number of rows this device occupies."""
        return max(len(self.meters), len(self.text))


def parse_bandwidth_value(value: str | int | float) -> float | None:
    """Parse bandwidth value with suffix to MB/s.

    Accepts:
        - "auto" -> None
        - 12GB -> 12000 MB/s
        - 300MB -> 300 MB/s
        - 100Gb -> 12500 MB/s
        - 300Mb -> 37.5 MB/s

    Strict: prefix must be uppercase (K/M/G/T), suffix B/b required.
    """
    if value is None or value == "auto":
        return None

    if isinstance(value, (int, float)):
        raise ValueError(f"Bandwidth value requires suffix (e.g. '100MB', '1Gb'): {value}")

    value = value.strip()
    if value.lower() == "auto":
        return None

    # Match pattern: number + prefix (K/M/G/T) + B or b
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT])([Bb])$', value)
    if not match:
        # Check for common errors
        if re.match(r'^(\d+(?:\.\d+)?)\s*([kmgt])([Bb])$', value):
            raise ValueError(f"Bandwidth prefix must be uppercase (K/M/G/T): {value}")
        if re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]?)$', value):
            raise ValueError(f"Bandwidth suffix B (bytes) or b (bits) required: {value}")
        raise ValueError(f"Invalid bandwidth format: {value}. Expected format like '100MB', '1Gb', '12GB'")

    num = float(match.group(1))
    prefix = match.group(2)
    is_bytes = match.group(3) == 'B'

    # Scale factors (using 1000-based, not 1024)
    scales = {'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12}
    scale = scales[prefix]

    # Convert to MB/s
    if is_bytes:
        # Already in bytes/sec, convert to MB/s
        return (num * scale) / 1e6
    else:
        # In bits/sec, convert to MB/s (divide by 8)
        return (num * scale) / 8 / 1e6


def parse_iops_value(value: str | int | float) -> float | None:
    """Parse IOPS value.

    Accepts:
        - "auto" -> None
        - integer/float -> value
        - "100K" -> 100000
        - "1M" -> 1000000
    """
    if value is None or value == "auto":
        return None

    if isinstance(value, (int, float)):
        return float(value)

    value = value.strip()
    if value.lower() == "auto":
        return None

    # Match pattern: number + optional prefix (K/M)
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KM])?$', value, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid IOPS format: {value}. Expected number or '100K', '1M'")

    num = float(match.group(1))
    prefix = match.group(2)

    if prefix:
        scales = {'K': 1e3, 'M': 1e6, 'k': 1e3, 'm': 1e6}
        num *= scales[prefix]

    return num


def parse_time_value(value: str | int | float | None) -> float | None:
    """Parse time value with suffix to seconds.

    Accepts (case-insensitive):
        - None or 0 -> None (disabled, all-time max)
        - "5m" -> 300 seconds
        - "300s" -> 300 seconds
        - "500ms" -> 0.5 seconds
        - "1h" -> 3600 seconds
        - integer/float -> seconds
    """
    if value is None or value == 0:
        return None

    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    value = value.strip().lower()
    if value == "0":
        return None

    # Check ms before m to avoid ambiguity
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(ms|s|m|h)$', value)
    if not match:
        raise ValueError(f"Invalid time format: {value}. Expected '5m', '300s', '500ms', or '1h'")

    num = float(match.group(1))
    unit = match.group(2)

    multipliers = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}
    return num * multipliers[unit]


def parse_temp_value(value: str | int | float, device: str | None = None) -> tuple[float, str]:
    """Parse temperature value with suffix to (celsius, unit).

    Accepts:
        - "33C" -> (33.0, "c")
        - "125F" -> (51.67, "c")  # converted to Celsius internally
        - "80%" -> (percentage of critical temp, "c")  # NVMe only
        - integer/float -> (value as Celsius, "c")

    Returns tuple of (value_in_celsius, original_unit).
    """
    if isinstance(value, (int, float)):
        return (float(value), "c")

    value = value.strip()

    # Check for percentage (NVMe critical temp)
    if value.endswith('%'):
        pct = float(value[:-1])
        # Get critical temp for device
        critical_temp = get_nvme_critical_temp(device) if device else None
        if critical_temp is None:
            raise ValueError(f"Cannot use % threshold - no critical temp found for {device}")
        return (critical_temp * pct / 100, "c")

    # Check for C/F suffix
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([CcFf])$', value)
    if not match:
        raise ValueError(f"Invalid temperature format: {value}. Expected '33C', '125F', or '80%'")

    num = float(match.group(1))
    unit = match.group(2).lower()

    if unit == 'f':
        # Convert Fahrenheit to Celsius for internal storage
        celsius = (num - 32) * 5 / 9
        return (celsius, "f")
    else:
        return (num, "c")


def parse_percentage_value(value: str | int | float) -> float:
    """Parse percentage value.

    Accepts:
        - "70%" -> 70.0
        - "90%" -> 90.0
        - integer/float -> value as percentage
    """
    if isinstance(value, (int, float)):
        return float(value)

    value = value.strip()
    if value.endswith('%'):
        return float(value[:-1])

    raise ValueError(f"Invalid percentage format: {value}. Expected '70%' or '90%'")


def parse_signal_value(value: str | int | float) -> float:
    """Parse signal strength value in dBm.

    Accepts:
        - "-50" -> -50.0
        - "-50dBm" -> -50.0
        - -50 -> -50.0
    """
    if isinstance(value, (int, float)):
        return float(value)

    value = value.strip().lower()
    if value.endswith('dbm'):
        value = value[:-3].strip()

    return float(value)


def parse_link_speed_value(value: str | int | float) -> float:
    """Parse link speed value in Mbps.

    Accepts:
        - 1000 -> 1000.0 (Mbps)
        - "100Mbps" -> 100.0
        - "1Gbps" -> 1000.0
        - "10Gbps" -> 10000.0
    """
    if isinstance(value, (int, float)):
        return float(value)

    value = value.strip()
    lower = value.lower()

    if lower.endswith('gbps'):
        return float(value[:-4].strip()) * 1000
    elif lower.endswith('mbps'):
        return float(value[:-4].strip())
    else:
        # Assume raw number in Mbps
        return float(value)


def parse_style(style_spec) -> str:
    """Parse style specification to Rich style string.

    Accepts:
        - "red" -> "red"
        - "red blink" -> "red blink"
        - {"color": "red", "blink": true} -> "red blink"
    """
    if isinstance(style_spec, str):
        return style_spec

    if isinstance(style_spec, dict):
        parts = []
        if "color" in style_spec:
            parts.append(style_spec["color"])
        if style_spec.get("blink"):
            parts.append("blink")
        if style_spec.get("bold"):
            parts.append("bold")
        if style_spec.get("dim"):
            parts.append("dim")
        return " ".join(parts) if parts else "white"

    raise ValueError(f"Invalid style format: {style_spec}")


# Default styles for thresholds (green -> yellow -> red -> red blink)
DEFAULT_THRESHOLD_STYLES = ["green", "yellow", "red", "red blink"]


def parse_thresholds(
    thresholds_spec,
    value_parser,
    device: str | None = None,
) -> list[Threshold]:
    """Parse threshold specification into list of Threshold objects.

    Accepts:
        - Short form: [33, 35, 37] or ["33C", "35C", "37C"]
        - Long form: [{val: null, style: "green"}, {val: "33C", style: "yellow"}, ...]
        - Mixed: allowed but not recommended

    value_parser: function to parse threshold values (parse_temp_value or parse_percentage_value)
    """
    if not thresholds_spec:
        return []

    thresholds = []

    # Check if first item is null - if not, we'll prepend one
    first_is_null = (
        (isinstance(thresholds_spec[0], dict) and thresholds_spec[0].get("val") is None) or
        thresholds_spec[0] is None
    )

    # Style offset: if we'll prepend null, user styles start at index 1
    style_offset = 0 if first_is_null else 1

    for i, item in enumerate(thresholds_spec):
        style_idx = i + style_offset

        if isinstance(item, dict):
            # Long form: {val: ..., style: ...}
            val_raw = item.get("val")
            if val_raw is None:
                val = None
            elif device and value_parser == parse_temp_value:
                val, _ = value_parser(val_raw, device)
            elif value_parser == parse_temp_value:
                val, _ = value_parser(val_raw)
            else:
                val = value_parser(val_raw)

            style = parse_style(item.get("style", DEFAULT_THRESHOLD_STYLES[min(style_idx, len(DEFAULT_THRESHOLD_STYLES) - 1)]))
            thresholds.append(Threshold(value=val, style=style))
        else:
            # Short form: just value, use default style
            if item is None:
                val = None
            elif device and value_parser == parse_temp_value:
                val, _ = value_parser(item, device)
            elif value_parser == parse_temp_value:
                val, _ = value_parser(item)
            else:
                val = value_parser(item)

            style = DEFAULT_THRESHOLD_STYLES[min(style_idx, len(DEFAULT_THRESHOLD_STYLES) - 1)]
            thresholds.append(Threshold(value=val, style=style))

    # Prepend null threshold if first one isn't null
    if thresholds and thresholds[0].value is not None:
        first_style = DEFAULT_THRESHOLD_STYLES[0]
        thresholds.insert(0, Threshold(value=None, style=first_style))

    return thresholds


def parse_text(text_spec, device_type: str = "disk", device: str | None = None, default_name: str | None = None) -> TextConfig:
    """Parse a text specification into TextConfig.

    Accepts:
        - "name" -> TextConfig(type="name", val=default_name, ...)
        - "usage" -> TextConfig(type="usage", thresholds=[default usage thresholds]) [disk only]
        - "temp" -> TextConfig(type="temp", thresholds=[default temp thresholds], downsample=10)
        - "signal" -> TextConfig(type="signal", thresholds=[default signal thresholds], inverted=True) [net only]
        - "ssid" -> TextConfig(type="ssid", ...) [net only, WiFi SSID]
        - "ip" -> TextConfig(type="ip", ...) [net only, IPv4 address]
        - "freq" -> TextConfig(type="freq", ...) [net only, WiFi frequency in GHz]
        - "link_speed" -> TextConfig(type="link_speed", ...) [net only, link speed in Mbps/Gbps]
        - "blank" -> TextConfig(type="blank", ...)
        - {"name": {"val": "Custom Name", "style": "cyan"}} -> TextConfig with custom name and style
        - {"ssid": {"style": "green"}} -> TextConfig with custom style
        - {"ip": {"style": {"color": "yellow", "dim": true}}} -> TextConfig with style dict
        - {"temp": {"thresholds": [...], "downsample": 5}} -> TextConfig with custom thresholds and downsample
        - {"usage": {"scale": 1.2, "offset": 5}} -> adjusted = computed * scale + offset
        - {"ssid": {"label": ""}} -> TextConfig with no label (value only)
        - {"signal": {"label": "dBm: "}} -> TextConfig with custom label (include colon if desired)
        - {"ssid": {"align": "left"}} -> TextConfig with left-aligned value (default is "right")
    """
    # Simple string form
    if isinstance(text_spec, str):
        text_type = text_spec
        text_opts = {}
    # Dict form: {"temp": {"thresholds": ...}}
    elif isinstance(text_spec, dict):
        if len(text_spec) != 1:
            raise ValueError(f"Text config must have exactly one key: {text_spec}")
        text_type = list(text_spec.keys())[0]
        text_opts = text_spec[text_type] or {}
    else:
        raise ValueError(f"Invalid text spec: {text_spec}")

    valid_types = {"name", "temp", "usage", "signal", "ssid", "ip", "freq", "link_speed", "blank"}
    if text_type not in valid_types:
        raise ValueError(f"Invalid text type: {text_type}. Valid: {valid_types}")

    # Validate text types for device type
    disk_only_types = {"usage"}
    net_only_types = {"signal", "ssid", "ip", "freq", "link_speed"}

    if device_type == "net" and text_type in disk_only_types:
        raise ValueError(f"Text type '{text_type}' not valid for net devices")
    if device_type != "net" and text_type in net_only_types:
        raise ValueError(f"Text type '{text_type}' only valid for net devices")

    # Parse type-specific options
    val = None
    downsample = 0
    inverted = False
    style = None
    scale = 1.0
    offset = 0.0
    label = text_opts.get("label")  # custom label (includes colon), None = use default
    align = text_opts.get("align", "right")  # value alignment: "left" or "right"

    if text_type == "name":
        val = text_opts.get("val", default_name)
        style = text_opts.get("style")
    elif text_type == "temp":
        downsample = text_opts.get("downsample", 10)  # default: poll every 10 refreshes
    elif text_type == "signal":
        inverted = True  # lower dBm values are worse
    elif text_type in ("ssid", "ip", "freq"):
        style = text_opts.get("style", "blue")
    elif text_type == "usage":
        scale = float(text_opts.get("scale", 1.0))
        offset = float(text_opts.get("offset", 0.0))

    # Parse style if provided
    if style is not None:
        style = parse_style(style)

    # Parse thresholds
    if text_type == "temp":
        thresholds_raw = text_opts.get("thresholds")
        if thresholds_raw:
            thresholds = parse_thresholds(thresholds_raw, parse_temp_value, device)
        else:
            # Default temp thresholds - try hwmon critical temp first
            critical_temp = get_nvme_critical_temp(device) if device else None
            if critical_temp is not None:
                # Use percentage of critical temp
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=critical_temp * 0.80, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=critical_temp * 0.95, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=critical_temp * 0.99, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
            elif device_type == "ssd":
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=55, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=65, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=75, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
            elif device_type == "net":
                # NIC temp thresholds
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=60, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=70, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=80, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
            else:  # hdd
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=45, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=55, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=60, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
    elif text_type == "usage":
        thresholds_raw = text_opts.get("thresholds")
        if thresholds_raw:
            thresholds = parse_thresholds(thresholds_raw, parse_percentage_value)
        else:
            ## Default usage thresholds based on device type
            ##
            ## These default usage color curves are chosen to try to help users
            ## understand performance, rather than to warn about actual capacity
            ## running low.
            ##
            ## HDDs perform faster at low usage because the outer sectors of the
            ## platters literally spin faster, which increases the rate at which
            ## the disk head flies over them. SSDs also degrade in performance as
            ## capacity fills up, but for a different reason. SSD controllers need
            ## spare capacity for active garbage collection, write amplification,
            ## and partial block management.
            ##
            ## According to Claude, HDD performance is a square-root curve w.r.t.
            ## usage. I believe this is correct. Claude provides the following:
            ##
            ## If you work through the math: at fill level f, you're operating at
            ## radius:
            ##
            ##    r = sqrt(R_outer² - f × (R_outer² - R_inner²))
            ##
            ## And performance is proportional to that r. So performance vs fill
            ## is a square root curve, not a line.
            ##
            ## Practically, for a typical 3.5" drive (inner radius ~25mm, outer ~48mm):
            ##
			##     Fill %	Approx Performance
            ##     ---------------------------
			##     0%	    100%
			##     25%	    90%
			##     50%	    80%
			##     75%	    66%
			##     100%	    52%
            ##
            ## Benchmarks running `fio` confirm that the innermost sectors on my
            ## Toshiba N300 Pros are almost exactly half as fast as the outermost.
            ##
            ## SSDs, on the other hand, have a performance degradation curve that
            ## is closer to a cliff. As long as there is enough spare capacity for
            ## the controller to do it's thing, everything should operate at full
            ## speed. And some drives reserve hidden capacity permanently for the
            ## controller, so it's hard to know where exactly the cliff is.
            ##
            ## For the most part, neither encryption nor raid nor LVM should
            ## effect any of this - they all map sectors through transparently.
            ##
            ## But of course in order for the color codes to correlate with
            ## performance there is an assumption: you must be reading from the
            ## most-recently-written sectors on the drive, and writing to either
            ## the same sectors or to new, previously-unused sectors (i.e.,
            ## filling up the drive further). If you fill up the drive to the
            ## red level and then issue reads only to old data on the outer
            ## sectors, performance will be fine. In fact on SSDs if you're only
            ## issuing reads, you'd probably be okay on any sectors. This
            ## assumption is therefore clearly not foolproof but is essentially
            ## the standard cache locality heuristic, and so should hold in many
            ## cases.
            ##
            ## One case where this assumption could really break down is with
            ## poorly planned partitions. If you split your HDD into 2 halves
            ## and dedicate the first half to a rarely-used mostly-empty backup
            ## partition, then your active partition will be getting inner-sector
            ## bandwidth while your usage reading remains low. The outer sectors
            ## aren't full; they're just getting skipped.
            if device_type == "ssd":
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=75, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=90, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=99, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
            else:  # hdd
                thresholds = [
                    Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                    Threshold(value=50, style=DEFAULT_THRESHOLD_STYLES[1]),
                    Threshold(value=80, style=DEFAULT_THRESHOLD_STYLES[2]),
                    Threshold(value=99, style=DEFAULT_THRESHOLD_STYLES[3]),
                ]
    elif text_type == "signal":
        thresholds_raw = text_opts.get("thresholds")
        if thresholds_raw:
            thresholds = parse_thresholds(thresholds_raw, parse_signal_value)
        else:
            # Default signal thresholds in dBm (inverted: lower is worse)
            # > -50 = green (excellent)
            # -50 to -60 = yellow (good)
            # -60 to -70 = red (weak)
            # <= -70 = red blink (very weak)
            thresholds = [
                Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[0]),
                Threshold(value=-50, style=DEFAULT_THRESHOLD_STYLES[1]),
                Threshold(value=-60, style=DEFAULT_THRESHOLD_STYLES[2]),
                Threshold(value=-70, style=DEFAULT_THRESHOLD_STYLES[3]),
            ]
    elif text_type == "link_speed":
        thresholds_raw = text_opts.get("thresholds")
        if thresholds_raw:
            thresholds = parse_thresholds(thresholds_raw, parse_link_speed_value)
        else:
            # Default link speed thresholds in Mbps (higher is better)
            # >= 10Gbps = green (excellent)
            # >= 1Gbps = yellow (good)
            # >= 100Mbps = red (slow)
            # < 100Mbps = red blink (misconfigured)
            thresholds = [
                Threshold(value=None, style=DEFAULT_THRESHOLD_STYLES[3]),
                Threshold(value=100, style=DEFAULT_THRESHOLD_STYLES[2]),
                Threshold(value=1000, style=DEFAULT_THRESHOLD_STYLES[1]),
                Threshold(value=10000, style=DEFAULT_THRESHOLD_STYLES[0]),
            ]
    else:
        thresholds = []

    return TextConfig(type=text_type, thresholds=thresholds, val=val, downsample=downsample, inverted=inverted, style=style, scale=scale, offset=offset, label=label, align=align)


def read_diskstats() -> dict[str, DiskStats]:
    """Read current stats from /proc/diskstats"""
    stats = {}
    timestamp = time.time()

    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 14:
                continue

            name = parts[2]
            # Fields: https://www.kernel.org/doc/Documentation/iostats.txt
            # Field 1: reads completed
            # Field 3: sectors read
            # Field 5: writes completed
            # Field 7: sectors written
            # Field 10: ms spent doing I/O
            stats[name] = DiskStats(
                reads_completed=int(parts[3]),
                writes_completed=int(parts[7]),
                sectors_read=int(parts[5]),
                sectors_written=int(parts[9]),
                io_ms=int(parts[12]),
                timestamp=timestamp,
            )

    return stats


def compute_metrics(
    prev: DiskStats,
    curr: DiskStats,
    config: DiskConfig
) -> DiskMetrics:
    """Compute metrics from two samples"""
    interval = curr.timestamp - prev.timestamp
    if interval <= 0:
        interval = 1.0

    # Sectors are typically 512 bytes
    sector_bytes = 512

    delta_read = curr.sectors_read - prev.sectors_read
    delta_write = curr.sectors_written - prev.sectors_written
    delta_io_ms = curr.io_ms - prev.io_ms
    delta_read_ops = curr.reads_completed - prev.reads_completed
    delta_write_ops = curr.writes_completed - prev.writes_completed

    read_mbps = (delta_read * sector_bytes) / (interval * 1024 * 1024)
    write_mbps = (delta_write * sector_bytes) / (interval * 1024 * 1024)
    total_mbps = read_mbps + write_mbps

    read_iops = delta_read_ops / interval
    write_iops = delta_write_ops / interval
    total_iops = read_iops + write_iops

    # %util = time spent doing I/O / total time
    util_pct = min(100.0, (delta_io_ms / (interval * 1000)) * 100)

    return DiskMetrics(
        util_pct=util_pct,
        read_mbps=read_mbps,
        write_mbps=write_mbps,
        total_mbps=total_mbps,
        read_iops=read_iops,
        write_iops=write_iops,
        total_iops=total_iops,
    )


def read_netstats() -> dict[str, NetStats]:
    """Read current stats from /sys/class/net/*/statistics/"""
    stats = {}
    timestamp = time.time()

    net_path = Path("/sys/class/net")
    if not net_path.exists():
        return stats

    for iface_path in net_path.iterdir():
        iface = iface_path.name
        stats_path = iface_path / "statistics"

        if not stats_path.exists():
            continue

        try:
            rx_bytes = int((stats_path / "rx_bytes").read_text().strip())
            tx_bytes = int((stats_path / "tx_bytes").read_text().strip())
            rx_packets = int((stats_path / "rx_packets").read_text().strip())
            tx_packets = int((stats_path / "tx_packets").read_text().strip())

            stats[iface] = NetStats(
                rx_bytes=rx_bytes,
                tx_bytes=tx_bytes,
                rx_packets=rx_packets,
                tx_packets=tx_packets,
                timestamp=timestamp,
            )
        except (OSError, ValueError):
            continue

    return stats


def compute_net_metrics(prev: NetStats, curr: NetStats) -> NetMetrics:
    """Compute network metrics from two samples"""
    interval = curr.timestamp - prev.timestamp
    if interval <= 0:
        interval = 1.0

    delta_rx = curr.rx_bytes - prev.rx_bytes
    delta_tx = curr.tx_bytes - prev.tx_bytes
    delta_rx_pkt = curr.rx_packets - prev.rx_packets
    delta_tx_pkt = curr.tx_packets - prev.tx_packets

    rx_mbps = delta_rx / (interval * 1024 * 1024)
    tx_mbps = delta_tx / (interval * 1024 * 1024)
    total_mbps = rx_mbps + tx_mbps

    rx_pps = delta_rx_pkt / interval
    tx_pps = delta_tx_pkt / interval
    total_pps = rx_pps + tx_pps

    return NetMetrics(
        rx_mbps=rx_mbps,
        tx_mbps=tx_mbps,
        total_mbps=total_mbps,
        rx_pps=rx_pps,
        tx_pps=tx_pps,
        total_pps=total_pps,
    )


def get_wifi_info(device: str) -> WifiInfo:
    """Get WiFi signal strength, SSID, and frequency with a single iw call.

    Tries /proc/net/wireless first for signal (faster, no subprocess),
    falls back to `iw dev` for newer drivers that don't populate /proc/net/wireless.

    Returns WifiInfo with signal_dbm, ssid, and freq_mhz (any may be None if not available).
    """
    signal_dbm: float | None = None
    ssid: str | None = None
    freq_mhz: float | None = None

    # Try /proc/net/wireless first for signal (faster, no subprocess)
    try:
        with open("/proc/net/wireless") as f:
            # Skip header lines
            next(f)
            next(f)

            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    # Interface name has trailing colon
                    iface = parts[0].rstrip(':')
                    if iface == device:
                        # Level is the third value (index 3), may have trailing period
                        level_str = parts[3].rstrip('.')
                        signal_dbm = float(level_str)
                        break
    except (OSError, StopIteration, ValueError, IndexError):
        pass

    # Call `iw dev` once to get SSID, freq (and signal if not found above)
    try:
        result = subprocess.run(
            ["iw", "dev", device, "link"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    # Format: "SSID: My Network Name"
                    ssid = line[5:].strip()
                elif line.startswith("freq:"):
                    # Format: "freq: 5180" or "freq: 5180.0"
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            freq_mhz = float(parts[1])
                        except ValueError:
                            pass
                elif signal_dbm is None and line.startswith("signal:"):
                    # Format: "signal: -50 dBm"
                    # Only use this if /proc/net/wireless didn't work
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            signal_dbm = float(parts[1])
                        except ValueError:
                            pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return WifiInfo(signal_dbm=signal_dbm, ssid=ssid, freq_mhz=freq_mhz)


def get_ipv4_address(device: str) -> str | None:
    """Get IPv4 address for a network interface.

    Returns None if device has no IPv4 address assigned.
    """
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", device],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    # Format: "inet 192.168.1.100/24 brd 192.168.1.255 scope global ..."
                    parts = line.split()
                    if len(parts) >= 2:
                        # Return just the IP, not the CIDR suffix
                        return parts[1].split('/')[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return None


def get_link_speed(device: str) -> int | None:
    """Get link speed for a network interface in Mbps.

    Returns None if speed is unavailable (e.g., link down, virtual interface).
    """
    try:
        speed_path = Path(f"/sys/class/net/{device}/speed")
        if speed_path.exists():
            speed = int(speed_path.read_text().strip())
            # -1 means speed is unknown (link down or not applicable)
            if speed > 0:
                return speed
    except (ValueError, OSError):
        pass

    return None


def get_nic_hwmon_path(device: str) -> Path | None:
    """Find hwmon path for a network interface.

    Some NICs expose temperature via hwmon at /sys/class/net/<device>/device/hwmon/hwmonX/
    """
    hwmon_dir = Path(f"/sys/class/net/{device}/device/hwmon")
    if not hwmon_dir.exists():
        return None

    # Find first hwmon subdirectory
    try:
        for entry in hwmon_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("hwmon"):
                return entry
    except OSError:
        pass

    return None


def get_nic_temp(device: str) -> float | None:
    """Get current temperature in Celsius for a network interface.

    Returns None if the NIC doesn't expose temperature via hwmon.
    """
    hwmon = get_nic_hwmon_path(device)
    if not hwmon:
        return None

    try:
        temp_input = hwmon / "temp1_input"
        if temp_input.exists():
            millidegrees = int(temp_input.read_text().strip())
            return millidegrees / 1000.0
    except (OSError, ValueError):
        pass

    return None


def get_swap_usage() -> tuple[int, int]:
    """Get swap used and total bytes from /proc/swaps"""
    total = 0
    used = 0
    try:
        with open("/proc/swaps") as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    # size and used are in KB
                    total += int(parts[2]) * 1024
                    used += int(parts[3]) * 1024
    except (OSError, StopIteration):
        pass
    return used, total


def get_device_size(device: str) -> int | None:
    """Get total device size in bytes from sysfs"""
    try:
        with open(f"/sys/block/{device}/size") as f:
            sectors = int(f.read().strip())
            return sectors * 512
    except (OSError, ValueError):
        return None


def get_nvme_hwmon_path(device: str) -> Path | None:
    """Find hwmon path for an NVMe device"""
    # device is like "nvme0n1" or "nvme0n1p1", we need "nvme0"
    match = re.match(r'(nvme\d+)', device)
    if not match:
        return None
    nvme_ctrl = match.group(1)
    pattern = f"/sys/class/nvme/{nvme_ctrl}/hwmon*"
    matches = glob.glob(pattern)
    if matches:
        return Path(matches[0])
    return None


def get_nvme_temp(device: str) -> float | None:
    """Get current temperature in Celsius for an NVMe device"""
    hwmon = get_nvme_hwmon_path(device)
    if not hwmon:
        return None
    try:
        with open(hwmon / "temp1_input") as f:
            millidegrees = int(f.read().strip())
            return millidegrees / 1000.0
    except (OSError, ValueError):
        return None


def get_sata_temp(device: str) -> float | None:
    """Get current temperature in Celsius for a SATA device via smartctl"""
    try:
        result = subprocess.run(
            ["sudo", "-n", "smartctl", "-A", f"/dev/{device}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # smartctl uses bitmask return codes, don't strictly check returncode
        # Just try to parse the output

        # Look for temperature in SMART attributes
        # Common attribute IDs: 194 (Temperature_Celsius), 190 (Airflow_Temperature_Cel)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 10:
                # Check for temperature attributes by ID or name
                attr_id = parts[0]
                attr_name = parts[1] if len(parts) > 1 else ""
                if attr_id in ("194", "190") or "temperature" in attr_name.lower():
                    # Temperature is typically the last numeric value (RAW_VALUE)
                    try:
                        # Handle cases like "35" or "35 (Min/Max 20/45)"
                        raw_value = parts[9].split()[0]
                        return float(raw_value)
                    except (ValueError, IndexError):
                        continue
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def get_disk_temp(device: str) -> float | None:
    """Get temperature for any disk type (NVMe or SATA)"""
    # Try NVMe first (faster, no subprocess)
    temp = get_nvme_temp(device)
    if temp is not None:
        return temp

    # Fall back to SATA/smartctl
    return get_sata_temp(device)


def get_nvme_temp_thresholds(device: str) -> tuple[float | None, float | None]:
    """Get max (warning) and critical temperature thresholds for an NVMe device"""
    hwmon = get_nvme_hwmon_path(device)
    if not hwmon:
        return None, None

    max_temp = None
    crit_temp = None

    try:
        with open(hwmon / "temp1_max") as f:
            max_temp = int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass

    try:
        with open(hwmon / "temp1_crit") as f:
            crit_temp = int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass

    return max_temp, crit_temp


def get_nvme_critical_temp(device: str) -> float | None:
    """Get critical temperature threshold for an NVMe device"""
    _, crit_temp = get_nvme_temp_thresholds(device)
    return crit_temp


def get_style_for_value(value: float, thresholds: list[Threshold], inverted: bool = False) -> Style:
    """Get style for a value based on threshold list.

    Thresholds should be ordered from lowest to highest value.
    First threshold with value=None is the default/base style.
    Returns the style of the highest threshold that the value exceeds.

    If inverted=True, uses <= comparison instead of >= (for signal strength
    where lower/more negative values are worse).
    """
    if not thresholds:
        return Style(color="white")

    # Find the highest threshold that value exceeds (or is below, if inverted)
    matched_style = thresholds[0].style  # default to first style

    for threshold in thresholds:
        if threshold.value is None:
            # Base style - always matches if no higher threshold matched
            matched_style = threshold.style
        elif inverted:
            if value <= threshold.value:
                matched_style = threshold.style
        else:
            if value >= threshold.value:
                matched_style = threshold.style

    # Parse style string to Style object
    return Style.parse(matched_style)


def validate_mount_points(mount_points: list[str]) -> None:
    """Validate that all mount points exist, raise error if not"""
    for mp in mount_points:
        if mp == "swap":
            continue
        if not Path(mp).exists():
            raise ValueError(f"Mount point does not exist: {mp}")


def get_capacity_pct(device: str, mount_points: list[str]) -> float | None:
    """Get disk capacity usage percentage (used from mount points, total from sysfs)"""
    if not mount_points:
        return None

    total_size = get_device_size(device)
    if not total_size:
        return None

    total_used = 0

    for mount_point in mount_points:
        if mount_point == "swap":
            used, _ = get_swap_usage()
            total_used += used
        else:
            try:
                usage = shutil.disk_usage(mount_point)
                total_used += usage.used
            except OSError:
                continue

    return (total_used / total_size) * 100


def render_bar(
    width: int,
    value: float,
    max_value: float = 100.0,
    char: str = "│",
    fill_style: Style = Style(color="green"),
) -> Text:
    """Render a single-color bar"""
    if max_value > 0:
        fill_pct = (value / max_value) * 100
    else:
        fill_pct = 0
    fill_pct = max(0, min(100, fill_pct))
    filled = int((fill_pct / 100) * width)
    empty = width - filled

    bar = Text()
    bar.append(char * filled, style=fill_style)
    bar.append(" " * empty)
    return bar


def render_bandwidth_bar(
    width: int,
    read_mbps: float,
    write_mbps: float,
    max_mbps: float,
    read_style: Style = Style(color="cyan"),
    write_style: Style = Style(color="magenta"),
) -> Text:
    """Render a two-color bar showing read/write distribution"""
    total = read_mbps + write_mbps

    if max_mbps <= 0:
        return Text(" " * width)

    read_pct = (read_mbps / max_mbps) * 100
    write_pct = (write_mbps / max_mbps) * 100

    read_chars = int((read_pct / 100) * width)
    write_chars = int((write_pct / 100) * width)

    # Clamp total to width
    if read_chars + write_chars > width:
        scale = width / (read_chars + write_chars)
        read_chars = int(read_chars * scale)
        write_chars = width - read_chars

    empty_chars = width - read_chars - write_chars

    bar = Text()
    bar.append("│" * read_chars, style=read_style)
    bar.append("│" * write_chars, style=write_style)
    bar.append(" " * empty_chars)
    return bar


def calc_text_widths(configs: dict[str, DiskConfig], columns: int) -> list[int]:
    """Calculate text column widths for each display column.

    This should be called once at startup to get stable widths.
    Fetches current SSID/IP values to determine widths.
    """
    devices = list(configs.keys())

    # Default labels for each text type
    default_labels = {
        "usage": "Usage: ",
        "temp": "Temp: ",
        "signal": "Signal: ",
        "ssid": "SSID: ",
        "ip": "IP: ",
        "freq": "Freq: ",
        "link_speed": "Link Speed: ",
    }

    def get_label(text_cfg: TextConfig) -> str:
        """Get the effective label for a text config (custom or default)"""
        if text_cfg.label is not None:
            return text_cfg.label
        return default_labels.get(text_cfg.type, "")

    # Fetch current text values for width calculation
    device_text_values: dict[str, dict] = {}
    for device in devices:
        cfg = configs[device]
        is_net = cfg.type == "net"

        values: dict = {
            "capacity_pct": None,
            "temp_c": None,
            "signal_dbm": None,
            "ssid": None,
            "ip_addr": None,
            "freq_mhz": None,
            "link_speed": None,
        }

        if not is_net:
            values["capacity_pct"] = get_capacity_pct(device, cfg.mount_points)
            values["temp_c"] = get_disk_temp(device)
        else:
            wifi_info = get_wifi_info(device)
            values["signal_dbm"] = wifi_info.signal_dbm
            values["ssid"] = wifi_info.ssid
            values["freq_mhz"] = wifi_info.freq_mhz
            values["ip_addr"] = get_ipv4_address(device)
            values["link_speed"] = get_link_speed(device)
            values["temp_c"] = get_nic_temp(device)

        device_text_values[device] = values

    def calc_device_text_width(device: str) -> int:
        cfg = configs[device]
        values = device_text_values[device]
        max_width = len(cfg.name)  # minimum is display name

        # Find max label length for this device
        max_label_len = 0
        for t in cfg.text:
            max_label_len = max(max_label_len, len(get_label(t)))

        # Calculate width for each text type
        for text_cfg in cfg.text:
            if text_cfg.type == "name":
                width = len(text_cfg.val or cfg.name)
            elif text_cfg.type in default_labels or text_cfg.label is not None:
                # prefix + label + value
                label = get_label(text_cfg)
                label_len = len(label)
                prefix_len = max_label_len + 2 - label_len

                if text_cfg.type == "usage" and values["capacity_pct"] is not None:
                    value_len = 4  # "100%"
                elif text_cfg.type == "temp" and values["temp_c"] is not None:
                    value_len = 5  # "100°F"
                elif text_cfg.type == "signal" and values["signal_dbm"] is not None:
                    value_len = 7  # "-100dBm"
                elif text_cfg.type == "ssid" and values["ssid"] is not None:
                    value_len = len(values["ssid"])
                elif text_cfg.type == "ip" and values["ip_addr"] is not None:
                    value_len = len(values["ip_addr"])
                elif text_cfg.type == "freq" and values["freq_mhz"] is not None:
                    value_len = 7  # "5.18GHz"
                elif text_cfg.type == "link_speed" and values["link_speed"] is not None:
                    value_len = 7  # "10Gbps"
                else:
                    continue  # no value, skip

                width = prefix_len + label_len + value_len
            else:
                width = 0
            max_width = max(max_width, width)

        return max_width

    # Calculate per-column text widths
    text_widths = [0] * columns
    for idx, device in enumerate(devices):
        col = idx % columns
        cfg = configs[device]

        # Auto-calculated width
        auto_width = calc_device_text_width(device)

        # Configured minimum (if any)
        min_width = cfg.text_width or 0

        text_widths[col] = max(text_widths[col], auto_width, min_width)

    return text_widths


def render_display(
    metrics: dict[str, DiskMetrics | NetMetrics],
    configs: dict[str, DiskConfig],
    console_width: int,
    columns: int = 1,
    refresh_counter: int = 0,
    temp_cache: dict[str, float | None] | None = None,
    observed_max: dict[str, float] | None = None,
    decaying_max: dict[str, float] | None = None,
    interval: float = 1.0,
    use_fahrenheit: bool = False,
    text_widths: list[int] | None = None,
) -> Table:
    """Render the full display"""
    if temp_cache is None:
        temp_cache = {}
    if observed_max is None:
        observed_max = {}
    if decaying_max is None:
        decaying_max = {}
    if text_widths is None:
        text_widths = calc_text_widths(configs, columns)

    devices = list(configs.keys())  # preserve config file order

    # Helper to get temp downsample from text config
    def get_temp_downsample(cfg: DiskConfig) -> int:
        for t in cfg.text:
            if t.type == "temp":
                return t.downsample
        return 0

    # Fetch current text values for rendering (not for width calculation)
    device_text_values: dict[str, dict] = {}
    for device in devices:
        cfg = configs[device]
        is_net = cfg.type == "net"
        temp_downsample = get_temp_downsample(cfg)

        values: dict = {
            "capacity_pct": None,
            "temp_c": None,
            "signal_dbm": None,
            "ssid": None,
            "ip_addr": None,
            "freq_mhz": None,
            "link_speed": None,
        }

        if not is_net:
            values["capacity_pct"] = get_capacity_pct(device, cfg.mount_points)
            if temp_downsample > 0 and refresh_counter % temp_downsample == 0:
                temp_cache[device] = get_disk_temp(device)
            values["temp_c"] = temp_cache.get(device) if temp_downsample > 0 else None
        else:
            wifi_info = get_wifi_info(device)
            values["signal_dbm"] = wifi_info.signal_dbm
            values["ssid"] = wifi_info.ssid
            values["freq_mhz"] = wifi_info.freq_mhz
            values["ip_addr"] = get_ipv4_address(device)
            values["link_speed"] = get_link_speed(device)
            if temp_downsample > 0 and refresh_counter % temp_downsample == 0:
                temp_cache[device] = get_nic_temp(device)
            values["temp_c"] = temp_cache.get(device) if temp_downsample > 0 else None

        device_text_values[device] = values

    # Default labels for each text type
    default_labels = {
        "usage": "Usage: ",
        "temp": "Temp: ",
        "signal": "Signal: ",
        "ssid": "SSID: ",
        "ip": "IP: ",
        "freq": "Freq: ",
        "link_speed": "Link Speed: ",
    }

    def get_label(text_cfg: TextConfig) -> str:
        """Get the effective label for a text config (custom or default)"""
        if text_cfg.label is not None:
            return text_cfg.label
        return default_labels.get(text_cfg.type, "")

    # Calculate width available per column
    column_width = console_width // columns

    label_width = 4
    padding = 6  # padding between columns

    # Outer table holds column groups
    outer_table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        padding=(0, 0),
        collapse_padding=True,
    )

    for _ in range(columns):
        outer_table.add_column(width=column_width)

    outer_table.add_row(*[""] * columns)  # blank line at top

    # Process devices in groups of `columns`
    for i in range(0, len(devices), columns):
        group = devices[i:i + columns]

        # Build a table for each device in the group
        device_tables = []
        for col_idx, device in enumerate(group):
            m = metrics[device]
            cfg = configs[device]
            is_net = cfg.type == "net"
            values = device_text_values[device]

            # Get pre-fetched values
            capacity_pct = values["capacity_pct"]
            temp_c = values["temp_c"]
            signal_dbm = values["signal_dbm"]
            ssid = values["ssid"]
            ip_addr = values["ip_addr"]
            freq_mhz = values["freq_mhz"]
            link_speed = values["link_speed"]

            # Use per-column text width
            device_width = text_widths[(i + col_idx) % columns]

            # Calculate bar width for this column
            bar_width = column_width - device_width - label_width - padding
            bar_width = max(20, bar_width)  # minimum bar width

            device_table = Table(
                show_header=False,
                show_edge=False,
                box=None,
                padding=(0, 1),
                collapse_padding=True,
            )

            device_table.add_column("device", style="bold white", width=device_width, overflow="ellipsis", no_wrap=True)
            device_table.add_column("label", style="bright_black", width=label_width, justify="right")
            device_table.add_column("bar", width=bar_width + 2)

            # Calculate max label width for this device's text config
            max_label_len = 0
            for t in cfg.text:
                max_label_len = max(max_label_len, len(get_label(t)))

            def get_label_prefix(label_len: int) -> str:
                """Get prefix spaces to right-align label with 2 char indent from name"""
                if label_len == 0:
                    return ""
                padding = max_label_len + 2 - label_len
                return " " * padding

            # Render each row - meters and text are now aligned by load_config padding
            for row_idx, (meter, text_cfg) in enumerate(zip(cfg.meters, cfg.text)):
                # Helper to build label with right-justified value
                def build_label_value(label: str, value: str | None, value_style=None, align: str = "right") -> Text:
                    prefix = get_label_prefix(len(label))
                    prefix_label_len = len(prefix) + len(label)
                    value_str = value or ""
                    # Calculate padding for alignment within device_width
                    padding_needed = device_width - prefix_label_len - len(value_str)
                    padding = " " * max(0, padding_needed)

                    result = Text(prefix)
                    result.append(label, style="bright_black")
                    if align == "left":
                        if value_str:
                            result.append(value_str, style=value_style)
                        result.append(padding)
                    else:
                        result.append(padding)
                        if value_str:
                            result.append(value_str, style=value_style)
                    return result

                # Build text column content
                if text_cfg.type == "blank":
                    device_col = ""
                elif text_cfg.type == "name":
                    name_val = text_cfg.val or cfg.name
                    if text_cfg.style:
                        device_col = Text(name_val, style=text_cfg.style)
                    else:
                        device_col = name_val  # use column default (bold white)
                elif text_cfg.type == "usage":
                    value_str = None
                    value_style = None
                    if capacity_pct is not None:
                        adjusted_pct = capacity_pct * text_cfg.scale + text_cfg.offset
                        value_style = get_style_for_value(adjusted_pct, text_cfg.thresholds)
                        value_str = f"{adjusted_pct:3.0f}%"
                    device_col = build_label_value(get_label(text_cfg), value_str, value_style, text_cfg.align)
                elif text_cfg.type == "temp":
                    value_str = None
                    value_style = None
                    if temp_c is not None:
                        value_style = get_style_for_value(temp_c, text_cfg.thresholds)
                        if use_fahrenheit:
                            temp_f = temp_c * 9 / 5 + 32
                            value_str = f"{temp_f:3.0f}°F"
                        else:
                            value_str = f"{temp_c:3.0f}°C"
                    device_col = build_label_value(get_label(text_cfg), value_str, value_style, text_cfg.align)
                elif text_cfg.type == "signal":
                    value_str = None
                    value_style = None
                    if signal_dbm is not None:
                        value_style = get_style_for_value(signal_dbm, text_cfg.thresholds, inverted=text_cfg.inverted)
                        value_str = f"{signal_dbm:3.0f}dBm"
                    device_col = build_label_value(get_label(text_cfg), value_str, value_style, text_cfg.align)
                elif text_cfg.type == "ssid":
                    device_col = build_label_value(get_label(text_cfg), ssid, text_cfg.style, text_cfg.align)
                elif text_cfg.type == "ip":
                    device_col = build_label_value(get_label(text_cfg), ip_addr, text_cfg.style, text_cfg.align)
                elif text_cfg.type == "freq":
                    freq_str = None
                    if freq_mhz is not None:
                        freq_ghz = freq_mhz / 1000
                        freq_str = f"{freq_ghz:.2f}GHz"
                    device_col = build_label_value(get_label(text_cfg), freq_str, text_cfg.style, text_cfg.align)
                elif text_cfg.type == "link_speed":
                    speed_str = None
                    value_style = None
                    if link_speed is not None:
                        value_style = get_style_for_value(link_speed, text_cfg.thresholds)
                        if link_speed >= 1000:
                            speed_str = f"{link_speed // 1000}Gbps"
                        else:
                            speed_str = f"{link_speed}Mbps"
                    device_col = build_label_value(get_label(text_cfg), speed_str, value_style, text_cfg.align)
                else:
                    device_col = ""

                # Build meter column content
                if meter.type == "blank":
                    device_table.add_row(device_col, "", "")
                    continue

                if meter.type == "utilization":
                    # Track observed max
                    util_key = f"{device}_util_{row_idx}"
                    observed_max[util_key] = max(observed_max.get(util_key, 0), m.util_pct)

                    max_util = meter.max_value if meter.max_value is not None else 100.0
                    util_bar = Text("[")
                    util_bar.append_text(render_bar(
                        bar_width,
                        m.util_pct,
                        max_value=max_util,
                        fill_style=Style(color=meter.color_in),
                    ))
                    util_bar.append("]")
                    device_table.add_row(device_col, meter.label, util_bar)

                elif meter.type == "iops":
                    # Track observed max with unique key per device and meter index
                    total_iops = m.read_iops + m.write_iops
                    iops_key = f"{device}_iops_{row_idx}"
                    observed_max[iops_key] = max(observed_max.get(iops_key, 0), total_iops)

                    # Determine effective max for display
                    if meter.max_value is not None:
                        # Explicit max configured
                        effective_max = meter.max_value
                    elif meter.halflife is not None:
                        # Auto with decay: apply exponential decay
                        decay_factor = 0.5 ** (interval / meter.halflife)
                        prev_decaying = decaying_max.get(iops_key, 0)
                        decaying_max[iops_key] = max(total_iops, prev_decaying * decay_factor)
                        effective_max = decaying_max[iops_key]
                    else:
                        # Auto without decay: use all-time max
                        effective_max = observed_max[iops_key]

                    iops_bar = Text("[")
                    if effective_max > 0:
                        iops_bar.append_text(render_bandwidth_bar(
                            bar_width,
                            m.read_iops,
                            m.write_iops,
                            effective_max,
                            read_style=Style(color=meter.color_in),
                            write_style=Style(color=meter.color_out),
                        ))
                    else:
                        iops_bar.append(" " * bar_width)
                    iops_bar.append("]")
                    device_table.add_row(device_col, meter.label, iops_bar)

                elif meter.type == "pps":
                    # PPS meter for network devices (like IOPS for disks)
                    total_pps = m.rx_pps + m.tx_pps
                    pps_key = f"{device}_pps_{row_idx}"
                    observed_max[pps_key] = max(observed_max.get(pps_key, 0), total_pps)

                    # Determine effective max for display
                    if meter.max_value is not None:
                        effective_max = meter.max_value
                    elif meter.halflife is not None:
                        decay_factor = 0.5 ** (interval / meter.halflife)
                        prev_decaying = decaying_max.get(pps_key, 0)
                        decaying_max[pps_key] = max(total_pps, prev_decaying * decay_factor)
                        effective_max = decaying_max[pps_key]
                    else:
                        effective_max = observed_max[pps_key]

                    pps_bar = Text("[")
                    if effective_max > 0:
                        pps_bar.append_text(render_bandwidth_bar(
                            bar_width,
                            m.rx_pps,
                            m.tx_pps,
                            effective_max,
                            read_style=Style(color=meter.color_in),
                            write_style=Style(color=meter.color_out),
                        ))
                    else:
                        pps_bar.append(" " * bar_width)
                    pps_bar.append("]")
                    device_table.add_row(device_col, meter.label, pps_bar)

                elif meter.type == "bandwidth":
                    # Use rx/tx for net, read/write for disk
                    if is_net:
                        in_val, out_val = m.rx_mbps, m.tx_mbps
                    else:
                        in_val, out_val = m.read_mbps, m.write_mbps

                    # Track observed max with unique key per device and meter index
                    total_bw = in_val + out_val
                    bw_key = f"{device}_bandwidth_{row_idx}"
                    observed_max[bw_key] = max(observed_max.get(bw_key, 0), total_bw)

                    # Determine effective max for display
                    if meter.max_value is not None:
                        # Explicit max configured
                        effective_max = meter.max_value
                    elif meter.halflife is not None:
                        # Auto with decay: apply exponential decay
                        decay_factor = 0.5 ** (interval / meter.halflife)
                        prev_decaying = decaying_max.get(bw_key, 0)
                        decaying_max[bw_key] = max(total_bw, prev_decaying * decay_factor)
                        effective_max = decaying_max[bw_key]
                    else:
                        # Auto without decay: use all-time max
                        effective_max = observed_max[bw_key]

                    bw_bar = Text("[")
                    if effective_max > 0:
                        bw_bar.append_text(render_bandwidth_bar(
                            bar_width,
                            in_val,
                            out_val,
                            effective_max,
                            read_style=Style(color=meter.color_in),
                            write_style=Style(color=meter.color_out),
                        ))
                    else:
                        bw_bar.append(" " * bar_width)
                    bw_bar.append("]")
                    device_table.add_row(device_col, meter.label, bw_bar)

            device_tables.append(device_table)

        # Pad with logo (once) if group is incomplete and logo fits
        logo_shown = False
        while len(device_tables) < columns:
            # Check if logo fits (need at least 3 rows in the device)
            first_device = devices[i]
            num_rows = len(configs[first_device].meters)

            if num_rows >= 3 and not logo_shown:
                # Create a table with the logo (no padding - logo has its own flair)
                logo_table = Table(
                    show_header=False,
                    show_edge=False,
                    box=None,
                    padding=(0, 0),
                    collapse_padding=True,
                )
                logo_table.add_column("logo")

                # Render scaled logo (subtract 2 for safety margin)
                logo_lines = render_logo(column_width - 0)

                # Add logo lines, padding if device has more than 3 rows
                for line_idx in range(num_rows):
                    if line_idx < len(logo_lines):
                        logo_table.add_row(logo_lines[line_idx])
                    else:
                        logo_table.add_row("")

                device_tables.append(logo_table)
                logo_shown = True
            else:
                device_tables.append(Text(""))

        outer_table.add_row(*device_tables)
        outer_table.add_row(*[""] * columns)  # spacing between groups

    return outer_table


def compute_display_height(configs: dict[str, DiskConfig], columns: int) -> int:
    """Compute the total display height in rows.

    Layout:
        - 1 blank line at top
        - For each group of devices (grouped by columns):
            - max(device.get_height()) rows for the tallest device in group
            - 1 blank line for spacing
    """
    devices = list(configs.keys())
    if not devices:
        return 0

    height = 1  # blank line at top

    # Process devices in groups of `columns`
    for i in range(0, len(devices), columns):
        group = devices[i:i + columns]
        # Height of this group is the max height among devices in the group
        group_height = max(configs[device].get_height() for device in group)
        height += group_height + 1  # +1 for spacing between groups

    return height


def parse_meter(meter_spec, device_type: str = "disk") -> MeterConfig:
    """Parse a meter specification into MeterConfig.

    Accepts:
        - "utilization" -> MeterConfig(type="utilization", label="util", max_value=100)
        - "iops" -> MeterConfig(type="iops", label="iops", max_value=None)  # auto
        - "bandwidth" -> MeterConfig(type="bandwidth", label="band", max_value=None)  # auto
        - "blank" -> MeterConfig(type="blank", label="", max_value=None)
        - {"iops": {"max": "100K"}} -> MeterConfig(type="iops", label="iops", max_value=100000)
        - {"bandwidth": {"label": "util", "max": "12GB"}} -> MeterConfig(...)
        - {"bandwidth": {"max": "auto", "halflife": "5m"}} -> MeterConfig with decay
        - {"bandwidth": {"color": "green"}} -> both read/write green
        - {"bandwidth": {"color": {"read": "cyan", "write": "magenta"}}} -> separate colors
    """
    default_labels = {
        "utilization": "util",
        "iops": "iops",
        "bandwidth": "band",
        "pps": "pps",
        "blank": "",
    }

    # Default colors by meter type
    default_colors = {
        "utilization": ("yellow", "yellow"),  # single color meter
        "iops": ("cyan", "magenta"),
        "bandwidth": ("cyan", "magenta"),
        "pps": ("cyan", "magenta"),
        "blank": ("white", "white"),
    }

    # Simple string form
    if isinstance(meter_spec, str):
        meter_type = meter_spec
        meter_opts = {}
    # Dict form: {"iops": {"max": ...}}
    elif isinstance(meter_spec, dict):
        if len(meter_spec) != 1:
            raise ValueError(f"Meter config must have exactly one key: {meter_spec}")
        meter_type = list(meter_spec.keys())[0]
        meter_opts = meter_spec[meter_type] or {}
    else:
        raise ValueError(f"Invalid meter spec: {meter_spec}")

    # Validate meter type for device type
    disk_meters = {"utilization", "iops", "bandwidth", "blank"}
    net_meters = {"bandwidth", "pps", "blank"}

    if device_type == "disk" and meter_type not in disk_meters:
        raise ValueError(f"Invalid meter type for disk: {meter_type}. Valid: {disk_meters}")
    elif device_type == "net" and meter_type not in net_meters:
        raise ValueError(f"Invalid meter type for net: {meter_type}. Valid: {net_meters}")

    # Get label
    label = meter_opts.get("label", default_labels.get(meter_type, meter_type[:4]))

    # Parse max value
    max_raw = meter_opts.get("max", "auto")

    if meter_type == "utilization":
        # Utilization is always 0-100%
        if max_raw == "auto" or max_raw is None:
            max_value = 100.0
        else:
            max_value = float(max_raw)
    elif meter_type == "bandwidth":
        max_value = parse_bandwidth_value(max_raw)
    elif meter_type in ("iops", "pps"):
        max_value = parse_iops_value(max_raw)
    else:  # blank
        max_value = None

    # Parse halflife (only meaningful for auto meters)
    halflife_raw = meter_opts.get("halflife")
    halflife = parse_time_value(halflife_raw)

    # Warn if halflife set on non-auto meter
    if halflife is not None and max_value is not None:
        raise ValueError(f"halflife only applies to auto meters (max: auto), got max={max_raw}")

    # Parse colors
    color_raw = meter_opts.get("color")
    default_in, default_out = default_colors.get(meter_type, ("cyan", "magenta"))

    if color_raw is None:
        # Use defaults
        color_in, color_out = default_in, default_out
    elif isinstance(color_raw, str):
        # Single color applies to both
        color_in, color_out = color_raw, color_raw
    elif isinstance(color_raw, dict):
        # Separate read/write colors
        if meter_type == "utilization":
            raise ValueError(f"utilization meter only supports a single color, not read/write: {color_raw}")
        color_in = color_raw.get("read", default_in)
        color_out = color_raw.get("write", default_out)
    else:
        raise ValueError(f"Invalid color format: {color_raw}. Expected string or {{read: ..., write: ...}}")

    return MeterConfig(
        type=meter_type, label=label, max_value=max_value, halflife=halflife,
        color_in=color_in, color_out=color_out,
    )


def load_config(path: Path) -> tuple[dict[str, DiskConfig], int]:
    """Load device configuration from YAML file

    Config format:
        devices:
          nvme0n1:
            type: ssd
            meters:
              - utilization
              - iops: { max: auto }
              - bandwidth: { max: 12GB }
            text:
              - name: { val: "My NVMe" }  # optional custom name
              - usage: { thresholds: [70%, 90%] }
              - temp: { downsample: 5 }  # optional custom downsample

          eth0:
            type: net
            # meters and text default appropriately for net
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    columns = data.get("columns", 1)

    configs = {}
    for device_key, cfg in data.get("devices", {}).items():
        if cfg is None:
            cfg = {}

        # Strip /dev/ prefix if present to get actual device name
        device = device_key.removeprefix("/dev/")

        # Use key as display name, allow override with 'name' at top level for backward compat
        display_name = cfg.get("name", device_key)

        # If given the device type, parse it.
        # Otherwise, take our best guess from the device name.
        if "type" in cfg:
            device_type = cfg["type"]
        elif device.startswith("sd"):
            device_type = "hdd"
        elif device.startswith("nvme"):
            device_type = "ssd"
        else:
            device_type = "net"

        # Determine if this is a network device
        is_net = device_type == "net"

        mount_points = cfg.get("mount_points", [])
        if isinstance(mount_points, str):
            mount_points = [mount_points] if mount_points else []

        # Parse meters with appropriate defaults
        if "meters" in cfg:
            meters_raw = cfg["meters"]
        elif is_net:
            # Net defaults: decaying bandwidth (util) in yellow, pps, all-time bandwidth
            meters_raw = [
                {"bandwidth": {"label": "util", "max": "auto", "halflife": "1m", "color": "yellow"}},
                "pps",
                "bandwidth",
            ]
        else:
            # Disk defaults: utilization, iops, bandwidth
            meters_raw = ["utilization", "iops", "bandwidth"]

        meter_device_type = "net" if is_net else "disk"
        meters = [parse_meter(m, meter_device_type) for m in meters_raw]

        # Parse text with appropriate defaults
        if "text" in cfg:
            text_raw = cfg["text"]
        elif is_net:
            # Net defaults: name, ssid, signal
            text_raw = ["name", "ssid", "signal"]
        else:
            # Disk defaults: name, usage, temp
            text_raw = ["name", "usage", "temp"]

        text_device_type = device_type or "disk"
        text = [parse_text(t, text_device_type, device, default_name=display_name) for t in text_raw]

        # Pad shorter list with blanks to match lengths
        while len(meters) < len(text):
            meters.append(MeterConfig(type="blank", label="", max_value=None, halflife=None, color_in="white", color_out="white"))
        while len(text) < len(meters):
            text.append(TextConfig(type="blank", thresholds=[], val=None, downsample=0))

        configs[device] = DiskConfig(
            type=device_type,
            meters=meters,
            text=text,
            mount_points=mount_points,
            name=display_name,
            text_width=cfg.get("text_width"),
        )

    return configs, columns


def format_time(seconds: float) -> str:
    """Format time in seconds to human-readable string (e.g., 5m, 300s, 500ms)"""
    if seconds >= 3600:
        return f"{seconds/3600:.0f}h"
    elif seconds >= 60:
        return f"{seconds/60:.0f}m"
    elif seconds >= 1:
        return f"{seconds:.0f}s"
    else:
        return f"{seconds*1000:.0f}ms"


def format_rate(value: float, unit_type: str) -> str:
    """Format a rate value with appropriate suffix for 3-5 digit display.

    unit_type: "bandwidth" for MB/s, GB/s etc., "iops" for I/s, KI/s etc., "pps" for P/s, KP/s etc.
    """
    if value == 0:
        if unit_type == "bandwidth":
            return "0 MB/s"
        elif unit_type == "pps":
            return "0  P/s"
        else:
            return "0  I/s"

    if unit_type == "bandwidth":
        # Convert MB/s to bytes/s
        bps = value * 1e6
        units = [(" B/s", 1), ("KB/s", 1e3), ("MB/s", 1e6), ("GB/s", 1e9), ("TB/s", 1e12)]
    elif unit_type == "pps":
        bps = value
        units = [(" P/s", 1), ("KP/s", 1e3), ("MP/s", 1e6), ("GP/s", 1e9)]
    else:  # iops
        bps = value
        units = [(" I/s", 1), ("KI/s", 1e3), ("MI/s", 1e6), ("GI/s", 1e9)]

    # Find unit that gives 3-5 digits (100 to 99999)
    for unit, divisor in reversed(units):
        val = bps / divisor
        if 100 <= val < 100000:
            return f"{val:.0f} {unit}"

    # Fallback for small values
    for unit, divisor in units:
        val = bps / divisor
        if val >= 1:
            return f"{val:.0f} {unit}"

    if unit_type == "bandwidth":
        return f"{value:.0f} MB/s"
    elif unit_type == "pps":
        return f"{value:.0f}  P/s"
    else:
        return f"{value:.0f}  I/s"

def main():
    parser = ArgumentParser(description=DESCRIPTION,
                            formatter_class=lambda prog: RTHF(prog, max_help_position=80))
    parser.add_argument(
        "-c", "--config",
        type=Path,
        metavar="/path/to/confg.yaml",
        default=Path.home() / ".config" / "chipchat" / "config.yaml",
        help=(
            "Path to configuration YAML.\n"
            "Default: ~/.config/chipchat/config.yaml"
        )
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=1.0,
        metavar="<float>",
        help="Refresh rate in seconds.",
    )
    parser.add_argument(
        "-f", "--fahrenheit",
        action="store_true",
        help="Display temperatures in Freedom units.",
    )
    parser.add_argument(
        "-y", "--height",
        action="store_true",
        help="Print the computed display height and exit.",
    )

    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config file not found: {args.config}")
        print(f"Create a config file with your device settings.")
        return

    configs, columns = load_config(args.config)

    if args.height:
        print(compute_display_height(configs, columns))
        return

    if not configs:
        print("No devices configured. Edit your config file.")
        return

    console = Console()

    # Separate disk and net configs
    disk_configs = {k: v for k, v in configs.items() if v.type != "net"}
    net_configs = {k: v for k, v in configs.items() if v.type == "net"}

    # Read initial stats
    prev_disk_stats = read_diskstats()
    prev_net_stats = read_netstats()

    # Filter to only configured devices that exist
    available_disks = set(prev_disk_stats.keys()) & set(disk_configs.keys())
    available_nets = set(prev_net_stats.keys()) & set(net_configs.keys())
    available = available_disks | available_nets

    if not available:
        print(f"None of the configured devices found")
        print(f"Configured disks: {list(disk_configs.keys())}")
        print(f"Available disks: {list(prev_disk_stats.keys())}")
        print(f"Configured nets: {list(net_configs.keys())}")
        print(f"Available nets: {list(prev_net_stats.keys())}")
        return

    configs = {k: v for k, v in configs.items() if k in available}

    # Validate mount points (only for disk devices)
    for device, cfg in configs.items():
        if cfg.type != "net":
            try:
                validate_mount_points(cfg.mount_points)
            except ValueError as e:
                print(f"Error in config for {device}: {e}")
                return

    time.sleep(args.interval)

    refresh_counter = 0
    temp_cache: dict[str, float | None] = {}
    observed_max: dict[str, float] = {}
    decaying_max: dict[str, float] = {}

    # Calculate text widths once at startup
    text_widths = calc_text_widths(configs, columns)

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        while True:
            try:
                curr_disk_stats = read_diskstats()
                curr_net_stats = read_netstats()

                metrics = {}
                for device, cfg in configs.items():
                    if cfg.type == "net":
                        if device in prev_net_stats and device in curr_net_stats:
                            metrics[device] = compute_net_metrics(
                                prev_net_stats[device],
                                curr_net_stats[device],
                            )
                    else:
                        if device in prev_disk_stats and device in curr_disk_stats:
                            metrics[device] = compute_metrics(
                                prev_disk_stats[device],
                                curr_disk_stats[device],
                                cfg,
                            )

                live.update(render_display(
                    metrics, configs, console.width, columns,
                    refresh_counter, temp_cache, observed_max,
                    decaying_max, args.interval, args.fahrenheit,
                    text_widths,
                ))

                prev_disk_stats = curr_disk_stats
                prev_net_stats = curr_net_stats
                refresh_counter += 1
                time.sleep(args.interval)

            except KeyboardInterrupt:
                break

    # Print observed peak values
    print("\nPeak Values:")
    print("-" * 50)
    for device, cfg in configs.items():
        # First pass: collect all formatted values to find max width
        lines = []
        for meter_idx, meter in enumerate(cfg.meters):
            if meter.type == "blank":
                continue

            if meter.type == "utilization":
                peak = observed_max.get(f"{device}_util_{meter_idx}", 0)
                # Util: empty value, percentage as suffix
                lines.append((meter.label, "", f"({peak:3.0f}%)"))

            elif meter.type == "bandwidth":
                peak = observed_max.get(f"{device}_bandwidth_{meter_idx}", 0)
                formatted = format_rate(peak, "bandwidth")
                if meter.max_value is None:
                    if meter.halflife is not None:
                        halflife_str = format_time(meter.halflife)
                        lines.append((meter.label, formatted, f"(auto, {halflife_str})"))
                    else:
                        lines.append((meter.label, formatted, "(auto)"))
                else:
                    pct = (peak / meter.max_value * 100) if meter.max_value > 0 else 0
                    lines.append((meter.label, formatted, f"({pct:3.0f}%)"))

            elif meter.type == "iops":
                peak = observed_max.get(f"{device}_iops_{meter_idx}", 0)
                formatted = format_rate(peak, "iops")
                if meter.max_value is None:
                    if meter.halflife is not None:
                        halflife_str = format_time(meter.halflife)
                        lines.append((meter.label, formatted, f"(auto, {halflife_str})"))
                    else:
                        lines.append((meter.label, formatted, "(auto)"))
                else:
                    pct = (peak / meter.max_value * 100) if meter.max_value > 0 else 0
                    lines.append((meter.label, formatted, f"({pct:3.0f}%)"))

            elif meter.type == "pps":
                peak = observed_max.get(f"{device}_pps_{meter_idx}", 0)
                formatted = format_rate(peak, "pps")
                if meter.max_value is None:
                    if meter.halflife is not None:
                        halflife_str = format_time(meter.halflife)
                        lines.append((meter.label, formatted, f"(auto, {halflife_str})"))
                    else:
                        lines.append((meter.label, formatted, "(auto)"))
                else:
                    pct = (peak / meter.max_value * 100) if meter.max_value > 0 else 0
                    lines.append((meter.label, formatted, f"({pct:3.0f}%)"))

        # Find max widths
        max_label = max(len(l[0]) for l in lines) if lines else 0
        max_value = max(len(l[1]) for l in lines) if lines else 0

        # Print device and aligned values (right-aligned values, then suffix)
        print(f"{cfg.name}:")
        for label, value, suffix in lines:
            label_pad = " " * (max_label - len(label))
            value_pad = value.rjust(max_value)
            print(f"  {label}:{label_pad} {value_pad} {suffix}")


if __name__ == "__main__": main()
