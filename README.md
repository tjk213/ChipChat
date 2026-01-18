<!-- ○═════════════════════════════════════════════════════════════════════○ -->
<!-- ○═════════════════════════════════════════════════════════════════════○ -->
<!-- ○═══  ██████╗██╗  ██╗██╗██████╗  ██████╗██╗  ██╗ █████╗ ████████╗ ════○ -->
<!--      ██╔════╝██║  ██║██║██╔══██╗██╔════╝██║  ██║██╔══██╗╚══██╔══╝       -->
<!--      ██║     ███████║██║██████╔╝██║     ███████║███████║   ██║          -->
<!--      ██║     ██╔══██║██║██╔═══╝ ██║     ██╔══██║██╔══██║   ██║          -->
<!--      ╚██████╗██║  ██║██║██║     ╚██████╗██║  ██║██║  ██║   ██║          -->
<!-- ○═══  ╚═════╝╚═╝  ╚═╝╚═╝╚═╝      ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝    ════○ -->
<!-- ○═════════════════════════════════════════════════════════════════════○ -->
<!-- ○═════════════════════════════════════════════════════════════════════○ -->
<!--         Copyright © 2026 Tyler J. Kenney. All rights reserved.          -->
<!-- ○═════════════════════════════════════════════════════════════════════○ -->

# ChipChat
![example](doc/example.png)

I/O monitor showing meters for disks and network interfaces.

## Installation

```bash
ChipChat % pip install -r requirements.txt
ChipChat % pip install -e .
```

## Configuration

Create a config file at `~/.config/chipchat/config.yaml`:

```bash
mkdir -p ~/.config/chipchat
cp config.example.yaml ~/.config/chipchat/config.yaml
```

Edit the config to specify your devices:

```yaml
columns: 2

devices:
  # Disk device - uses defaults (name, usage, temp + util, iops, bandwidth)
  nvme0n1:
    type: ssd
    mount_points:
      - /

  # Network device - uses defaults (name, ssid, signal + bandwidth, pps, bandwidth)
  wlan0:
    type: net
```

### Meter Configuration

Each device can have 1-3 meters. Defaults vary by device type.

**Disk meters:**
```yaml
meters:
  - utilization                        # %util (yellow)
  - iops: { max: 100K }                # explicit max
  - bandwidth: { max: 12GB }           # explicit max in GB/s
  - bandwidth: { label: bw, max: auto } # custom label, auto-scaling
  - blank                              # empty row for spacing
```

**Network meters:**
```yaml
meters:
  - bandwidth                          # rx/tx bandwidth
  - pps                                # packets per second
  - utilization: { halflife: 1m }      # decaying bandwidth meter
```

### Text Configuration

**Disk text:**
```yaml
text:
  - name
  - usage: { thresholds: [50%, 80%, 99%] }
  - temp: { downsample: 10 }
```

**Network text:**
```yaml
text:
  - name
  - ssid: { style: "cyan" }
  - signal: { thresholds: [-50, -60, -70] }
  - ip: { style: "green" }
```

### Bandwidth Units

Prefix must be uppercase (K/M/G/T), suffix B (bytes) or b (bits):

| Format | Meaning |
|--------|---------|
| `12GB` | 12 gigabytes/sec |
| `300MB` | 300 megabytes/sec |
| `1Gb` | 1 gigabit/sec (= 125 MB/s) |
| `100Mb` | 100 megabits/sec (= 12.5 MB/s) |
| `auto` | Auto-scale based on observed peak |

### IOPS Units

```yaml
max: 100000   # plain number
max: 100K     # 100,000
max: 1.5M     # 1,500,000
max: auto     # auto-scale
```

To find your device names:
```bash
# Disks
lsblk
# or
cat /proc/diskstats | awk '{print $3}'

# Network interfaces
ip link
```

## Usage

```bash
python chipchat.py              # default 1s interval
python chipchat.py -i 0.5       # 500ms interval
python chipchat.py -f           # temperatures in Fahrenheit
python chipchat.py -c myconfig.yaml  # custom config path
```

Press Ctrl+C to exit. On exit, observed peak values are printed (useful for calibrating auto-scaling configs).

## Display

```
nvme0n1       util [████████████████              ]  wlan0          util [██████████████████████████    ]
  Usage: 45%  iops [██████▒▒▒▒                    ]    Signal: -52dBm  pps [████████████                  ]
   Temp: 52°C band [████████████████████▒▒▒▒▒▒    ]      SSID: MyNet band [██████████████████████████████]
```

- **util**: Percentage of time the disk was busy (yellow) or decaying bandwidth for net
- **iops/pps**: I/O operations or packets per second with read/rx (cyan) / write/tx (magenta)
- **band**: Bandwidth with read/rx (cyan) / write/tx (magenta)
- **Usage**: Capacity percentage, color-coded by thresholds
- **Temp**: Temperature (NVMe via sysfs, SATA via smartctl)
- **Signal**: WiFi signal strength in dBm, color-coded by thresholds
- **SSID**: Connected WiFi network name
- **IP**: IPv4 address

## Interpreting the meters

| Pattern | Meaning |
|---------|---------|
| High %util, low bandwidth | Small random I/O or high latency |
| Low %util, high bandwidth | Efficient large sequential transfers |
| High %util, low IOPS | Large block sizes, bandwidth-bound |
| High IOPS, low bandwidth | Small block sizes (4K), IOPS-bound |

## Notes

- For HDDs and SATA SSDs, bandwidth and IOPS are shared between read/write (half-duplex)
- For NVMe, read and write can mostly happen simultaneously (full-duplex)
- WiFi is half-duplex - rx and tx are time-multiplexed
- Ethernet is full-duplex - rx and tx can happen simultaneously
- HDD IOPS are typically 100-500, SATA SSD ~90K, NVMe can exceed 1M
- SATA temperature requires `smartctl` with passwordless sudo
