# diskrefresh

A **Victoria-like TUI** for HDD/SSD surface diagnostics on Linux. Pure Python + `curses`, **no external Python packages**. It is a comfortable front-end over the kernel's own block I/O (and `smartctl` for SMART), built for refreshing disk surfaces and finding slow/bad sectors — including on the MBR / EFI / system areas that Windows tools now refuse to touch.

```
sudo python3 diskrefresh.py
```

> Root is required for raw device access. The program runs without root but disk open will fail.


## Installation & Usage

> Ensure you have python >= 3.6 installed.

To install `diskrefresh`, pull the repository:

```
git clone https://github.com/apprsu/diskrefresh
cd diskrefresh
```

After that, you can already use this tool.

```
./diskrefresh.py
```

To be able to run `diskrefresh` from everywhere, add this tool to your PATH.

```
sudo ln -rs diskrefresh.py /usr/bin/diskrefresh
```
or
```
ln -rs diskrefresh.py /home/approx/.local/bin/diskrefresh
```
## Tabs

**SMART** — parsed summary (health, model, serial, temperature, power-on hours, critical attributes like Reallocated/Pending/Uncorrectable highlighted red) plus the full scrollable `smartctl -a` output. Press `r` to (re)load.

**Check & Repair** — a colored block grid (latency heat-map) on the left, a control panel + per-bucket counters on the right, and a live log at the bottom. A progress bar sits above the grid. Blocks fill left→right, top→bottom as they are processed, coloured by access time.

### Reading the grid (two views — press `m` to switch)

A terminal can't show one cell per block on a multi-TB disk, so there are two views, toggled with `m` (also works mid-run) or the **Grid view** field:

* **SCAN** (default) — **1 cell = 1 block**. You see every individual block at full resolution; the panel shows `page p/P` and the block range on screen. Best for actually watching the surface go by.
* **MAP** — the **whole disk** as one proportional heat-map. Each cell aggregates many blocks (the panel shows `1 cell ≈ N blocks`), and the coloured fraction of the grid equals the % done. Best for an at-a-glance overview of where the slow or bad regions are.

The progress bar and the `Progress`/`Blocks` lines always reflect true position regardless of view.

## Controls (Check & Repair)

| Field       | Meaning                                                                              |
| ----------- | ------------------------------------------------------------------------------------ |
| Preset      | Colour↔latency mapping (←/→ to switch, see `presets/`)                               |
| Range start | First byte to work on (default `0`). Accepts `4M`, `1G`, raw bytes                   |
| Range end   | Last byte (default = full disk size)                                                 |
| Block size  | I/O chunk, e.g. `4M`. Edit freely or ←/→ for presets                                 |
| Operation   | **VERIFY** (read+discard), **READ** (read, optional dump-to-file), **WRITE**         |
| Write fill  | `ZERO` or `RANDOM` (only for WRITE)                                                  |
| Dump file   | Where READ writes a raw dump (only for READ)                                         |
| Direct I/O  | `O_DIRECT` to bypass cache for honest timings (auto-falls back)                      |
| Grid view   | **SCAN** (1 cell = 1 block, paged) or **MAP** (whole-disk heat-map). Toggle with `m` |

## Safety

* **Devices are addressed via `/dev/disk/by-id/`** (WWN/serial), never the volatile `/dev/sdX`, so a disk that gets re-lettered on reboot can't be wiped by mistake. The `/dev/sdX` name is shown for reference only.
* **WRITE asks for confirmation** and makes you **type the disk id** to proceed. If any partition of the target is mounted, it warns explicitly.
* **Bad sectors don't crash it.** I/O errors (EIO) are caught, logged with the offending offset, and the run continues. Very slow reads land in the `timeout` colour bucket instead of blocking the UI (I/O runs in a worker thread).

## Colour presets

Latency→colour rules live in `presets/*.ini` and are hot-selectable in the UI. Edit `default.ini` or drop in your own — any `.ini` in that folder appears in the Preset selector. Example bucket section:

```ini
[buckets]
100  = green
200  = light_green
500  = yellow
1000 = orange
5000 = red
timeout = magenta
error   = white on dark_red
```

A block's measured latency is matched to the first bucket bound it is `<=`; anything slower than the last bound is `timeout`; an EIO read is `error`.

For a plain full-disk wipe you can of course still use `shred -v /dev/disk/by-id/…` or `dd` directly — this tool is the interactive, per-block-visual companion to them.

## Requirements

* Linux, Python 3.6+ (tested on 3.12), a 256-colour terminal recommended.
* `smartmontools` for the SMART tab (`apt install smartmontools`).
* Root for raw device I/O.
