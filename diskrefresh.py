#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import locale
import mmap
import array
import errno
import fcntl
import struct
import threading
import subprocess
import configparser
import curses
from collections import deque

# --- ioctl constants for block devices -------------------------------------
BLKGETSIZE64 = 0x80081272  # returns size in bytes (unsigned long long)
ALIGN = 4096               # alignment requirement for O_DIRECT

PRESET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")

# Sentinels stored in the per-block results array (array of unsigned bytes).
UNPROCESSED = 255


# ===========================================================================
#  Helpers
# ===========================================================================
def human_size(n):
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0 or unit == "P":
            if unit == "B":
                return "%d B" % int(n)
            return "%.2f %sB" % (n, unit)
        n /= 1024.0


def parse_size(s):
    """Parse '4M', '1G', '512K', '1000204886016' -> int bytes. Raises ValueError."""
    s = str(s).strip().upper().replace(" ", "")
    if not s:
        raise ValueError("empty")
    mult = 1
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    if s[-1] in suffixes:
        mult = suffixes[s[-1]]
        s = s[:-1]
        if s.endswith("I"):  # allow KiB style
            s = s[:-1]
    val = float(s)
    return int(val * mult)


def run_cmd(args, timeout=30):
    """Run a command, return (rc, stdout, stderr). Never raises on non-zero."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "command not found: %s" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout running: %s" % " ".join(args)
    except Exception as e:  # noqa
        return 1, "", str(e)


# ===========================================================================
#  Device discovery (by-id only)
# ===========================================================================
class Disk:
    def __init__(self, by_id, real, size, model, serial):
        self.by_id = by_id      # full /dev/disk/by-id/... path  (what we USE)
        self.real = real        # resolved /dev/sdX             (for display only)
        self.size = size
        self.model = model
        self.serial = serial

    @property
    def id_name(self):
        return os.path.basename(self.by_id)

    @property
    def kname(self):
        return os.path.basename(self.real)


def _disk_size_bytes(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            buf = fcntl.ioctl(fd, BLKGETSIZE64, b"\0" * 8)
            return struct.unpack("Q", buf)[0]
        finally:
            os.close(fd)
    except OSError:
        # fallback to /sys/block/<kname>/size (512-byte sectors)
        try:
            kname = os.path.basename(os.path.realpath(path))
            with open("/sys/block/%s/size" % kname) as f:
                return int(f.read().strip()) * 512
        except Exception:
            return 0


def discover_disks():
    """Return list[Disk] for *whole* disks, identified via /dev/disk/by-id/."""
    base = "/dev/disk/by-id"
    disks = {}  # keyed by real device to dedupe; prefer wwn-/nvme-/ata- names
    if not os.path.isdir(base):
        return []
    # priority of id schemes (higher = preferred for display)
    def prio(name):
        for i, pref in enumerate(("wwn-", "nvme-eui.", "nvme-", "ata-", "scsi-", "usb-")):
            if name.startswith(pref):
                return len(("wwn-", "nvme-eui.", "nvme-", "ata-", "scsi-", "usb-")) - i
        return 0

    for entry in sorted(os.listdir(base)):
        # skip partitions (…-part1, …-part2)
        if "-part" in entry:
            continue
        full = os.path.join(base, entry)
        try:
            real = os.path.realpath(full)
        except OSError:
            continue
        if not real.startswith("/dev/"):
            continue
        kname = os.path.basename(real)
        # only whole disks: must exist as /sys/block/<kname>
        if not os.path.isdir("/sys/block/%s" % kname):
            continue
        size = _disk_size_bytes(full)
        if size <= 0:
            continue
        cur = disks.get(real)
        if cur is None or prio(entry) > prio(cur.id_name):
            model = _sys_attr(kname, "device/model") or _sys_attr(kname, "device/name") or "?"
            # serial: try to pull from id name
            serial = ""
            for tag in ("_", "-"):
                pass
            disks[real] = Disk(full, real, size, model.strip(), serial)
    return list(disks.values())


def _sys_attr(kname, rel):
    try:
        with open("/sys/block/%s/%s" % (kname, rel)) as f:
            return f.read().strip()
    except Exception:
        return ""


def mounted_partitions(disk):
    """Return list of mounted source paths belonging to this disk (safety check)."""
    out = []
    kname = disk.kname
    try:
        with open("/proc/mounts") as f:
            for line in f:
                src = line.split()[0]
                if not src.startswith("/dev/"):
                    continue
                rk = os.path.basename(os.path.realpath(src))
                # partition of our disk? e.g. sda1 belongs to sda, nvme0n1p1 to nvme0n1
                if rk == kname or rk.startswith(kname) and rk[len(kname):].lstrip("p").isdigit():
                    out.append(src)
    except Exception:
        pass
    return out


# ===========================================================================
#  SMART
# ===========================================================================
CRITICAL_SMART = {
    "Reallocated_Sector_Ct",
    "Current_Pending_Sector",
    "Offline_Uncorrectable",
    "Reported_Uncorrect",
    "UDMA_CRC_Error_Count",
    "Reallocated_Event_Count",
}


class SmartInfo:
    def __init__(self):
        self.available = False
        self.health = "?"
        self.model = ""
        self.serial = ""
        self.firmware = ""
        self.capacity = ""
        self.rotation = ""
        self.temperature = ""
        self.power_on = ""
        self.criticals = []      # list of (name, raw, bad_bool)
        self.raw_text = ""       # full smartctl -a output
        self.error = ""


def read_smart(disk):
    info = SmartInfo()
    rc, out, err = run_cmd(["smartctl", "-a", disk.by_id], timeout=40)
    if rc == 127:
        info.error = "smartctl not found — install smartmontools (apt install smartmontools)"
        return info
    info.raw_text = out if out.strip() else err
    info.available = bool(out.strip())
    for line in out.splitlines():
        l = line.strip()
        low = l.lower()
        if "overall-health" in low and "result" in low:
            info.health = l.split(":", 1)[1].strip() if ":" in l else l
        elif low.startswith("device model") or low.startswith("model number"):
            info.model = l.split(":", 1)[1].strip()
        elif low.startswith("serial number"):
            info.serial = l.split(":", 1)[1].strip()
        elif low.startswith("firmware version"):
            info.firmware = l.split(":", 1)[1].strip()
        elif low.startswith("user capacity") or low.startswith("total nvm capacity"):
            info.capacity = l.split(":", 1)[1].strip()
        elif low.startswith("rotation rate"):
            info.rotation = l.split(":", 1)[1].strip()
        elif low.startswith("power_on_hours") or low.startswith("power on hours"):
            info.power_on = l.split(":", 1)[1].strip()
        # ATA attribute table rows: ID# NAME FLAG VAL WORST THRESH TYPE UPDATED WHEN_FAILED RAW
        parts = l.split()
        if len(parts) >= 10 and parts[0].isdigit():
            name = parts[1]
            raw = parts[-1]
            if name in CRITICAL_SMART:
                bad = False
                try:
                    bad = int(raw.split()[0]) > 0
                except Exception:
                    bad = False
                info.criticals.append((name, raw, bad))
            if name == "Temperature_Celsius" and not info.temperature:
                info.temperature = raw
            if name == "Power_On_Hours" and not info.power_on:
                info.power_on = raw
        # NVMe-style lines
        if low.startswith("temperature:") and not info.temperature:
            info.temperature = l.split(":", 1)[1].strip()
        if low.startswith("power on hours") and not info.power_on:
            info.power_on = l.split(":", 1)[1].strip()
    return info


# ===========================================================================
#  Colour presets
# ===========================================================================
# Named palette -> (xterm-256 index, basic-8 fallback colour constant)
PALETTE = {
    "grey":        (240, curses.COLOR_WHITE),
    "gray":        (240, curses.COLOR_WHITE),
    "green":       (40,  curses.COLOR_GREEN),
    "light_green": (118, curses.COLOR_GREEN),
    "dark_green":  (22,  curses.COLOR_GREEN),
    "yellow":      (226, curses.COLOR_YELLOW),
    "orange":      (208, curses.COLOR_YELLOW),
    "red":         (196, curses.COLOR_RED),
    "dark_red":    (88,  curses.COLOR_RED),
    "magenta":     (201, curses.COLOR_MAGENTA),
    "purple":      (93,  curses.COLOR_MAGENTA),
    "blue":        (33,  curses.COLOR_BLUE),
    "cyan":        (51,  curses.COLOR_CYAN),
    "white":       (15,  curses.COLOR_WHITE),
    "black":       (16,  curses.COLOR_BLACK),
}


class Preset:
    """An ordered latency->colour mapping loaded from an .ini file."""
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.unit = "ms"
        self.buckets = []        # list of (upper_bound_ms, colour_name)
        self.timeout_color = "magenta"
        self.error_color = "white"      # foreground for the bad-block marker
        self.error_bg = "dark_red"

    @property
    def labels(self):
        """Human labels for each index, in order: buckets…, timeout, error."""
        out = []
        lo = 0
        for b, _ in self.buckets:
            out.append("%d-%dms" % (lo, b))
            lo = b
        out.append(">%dms" % (self.buckets[-1][0] if self.buckets else 0))  # timeout
        out.append("I/O error")
        return out

    def classify(self, elapsed_ms, is_error):
        """Return result-index for the per-block array."""
        K = len(self.buckets)
        if is_error:
            return K + 1
        for i, (bound, _) in enumerate(self.buckets):
            if elapsed_ms <= bound:
                return i
        return K          # timeout bucket

    def color_name(self, index):
        K = len(self.buckets)
        if index == UNPROCESSED:
            return "grey"
        if index < K:
            return self.buckets[index][1]
        if index == K:
            return self.timeout_color
        return self.error_color  # error (special bg)

    @property
    def timeout_index(self):
        return len(self.buckets)

    @property
    def error_index(self):
        return len(self.buckets) + 1


def load_preset(path):
    cp = configparser.ConfigParser()
    cp.read(path)
    name = path
    if cp.has_option("meta", "name"):
        name = cp.get("meta", "name")
    p = Preset(name, path)
    if cp.has_option("meta", "unit"):
        p.unit = cp.get("meta", "unit")
    if cp.has_section("buckets"):
        items = []
        for key, val in cp.items("buckets"):
            key = key.strip().lower()
            val = val.strip().lower()
            if key in ("timeout",):
                p.timeout_color = val
            elif key in ("error", "bad"):
                # allow "white on dark_red" or "white/dark_red" or just fg
                if " on " in val:
                    fg, bg = val.split(" on ", 1)
                elif "/" in val:
                    fg, bg = val.split("/", 1)
                else:
                    fg, bg = val, "dark_red"
                p.error_color = fg.strip()
                p.error_bg = bg.strip()
            else:
                try:
                    bound = float(key)
                except ValueError:
                    continue
                items.append((bound, val))
        items.sort(key=lambda x: x[0])
        p.buckets = items
    if not p.buckets:
        # sane default if file was malformed
        p.buckets = [(100, "green"), (200, "light_green"), (500, "yellow"),
                     (1000, "orange"), (5000, "red")]
    return p


def discover_presets():
    out = []
    if os.path.isdir(PRESET_DIR):
        for f in sorted(os.listdir(PRESET_DIR)):
            if f.endswith(".ini"):
                out.append(os.path.join(PRESET_DIR, f))
    return out


# ===========================================================================
#  I/O engine (runs in a worker thread; never crashes the UI on bad sectors)
# ===========================================================================
class Session:
    """Shared state between the I/O worker and the UI, guarded by a lock."""
    def __init__(self, disk, start, end, block_size, op, write_mode,
                 dump_path, use_direct, preset):
        self.disk = disk
        self.start = start
        self.end = end
        self.block_size = block_size
        self.op = op                 # "VERIFY" | "READ" | "WRITE"
        self.write_mode = write_mode  # "ZERO" | "RANDOM"
        self.dump_path = dump_path
        self.use_direct = use_direct
        self.preset = preset

        self.total_blocks = max(1, (end - start + block_size - 1) // block_size)
        self.results = array.array("B", [UNPROCESSED]) * self.total_blocks
        n_idx = preset.error_index + 1
        self.counters = [0] * n_idx          # count per result index (buckets+timeout+error)
        self.processed = 0
        self.bad_offsets = []                # list of byte offsets that failed
        self.bytes_done = 0
        self.io_seconds = 0.0
        self.cur_index = 0
        self.status = "running"              # running|paused|stopping|done|error|aborted
        self.message = ""
        self.log = deque(maxlen=2000)
        self.lock = threading.Lock()
        self._pause = threading.Event()
        self._stop = threading.Event()
        self.started_at = time.time()

    # -- control --
    def pause(self):
        self._pause.set()
        self.add_log("info", "Paused")

    def resume(self):
        self._pause.clear()
        self.add_log("info", "Resumed")

    def request_stop(self):
        self._stop.set()
        self._pause.clear()

    def is_paused(self):
        return self._pause.is_set()

    def add_log(self, level, msg):
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.log.append((ts, level, msg))

    def snapshot_counts(self):
        with self.lock:
            return list(self.counters), self.processed, self.total_blocks


def _open_device(path, want_direct):
    """Open device read or read-write. Returns (fd_direct_or_None, fd_buffered)."""
    fdd = None
    if want_direct:
        try:
            fdd = os.open(path, os.O_RDWR | os.O_DIRECT)
        except OSError:
            fdd = None
    fdb = os.open(path, os.O_RDWR)
    return fdd, fdb


def io_worker(session):
    """The actual block loop. Catches every per-block error and keeps going."""
    s = session
    p = s.preset
    bs = s.block_size
    path = s.disk.by_id
    op = s.op
    dumpf = None

    # direct I/O only makes sense when offset & length are aligned
    aligned = (s.start % ALIGN == 0) and (bs % ALIGN == 0)
    want_direct = s.use_direct and aligned
    if s.use_direct and not aligned:
        s.add_log("warn", "Direct I/O disabled: start/block not %d-aligned" % ALIGN)

    fdd = fdb = None
    direct_buf = None
    write_payload = None
    try:
        try:
            fdd, fdb = _open_device(path, want_direct)
        except OSError as e:
            s.add_log("error", "open(%s) failed: %s" % (path, os.strerror(e.errno)))
            with s.lock:
                s.status = "error"
                s.message = "open failed: %s" % os.strerror(e.errno)
            return

        if want_direct and fdd is None:
            s.add_log("warn", "O_DIRECT unsupported here — using buffered + fadvise(DONTNEED)")
            want_direct = False

        if want_direct:
            direct_buf = mmap.mmap(-1, bs)  # page-aligned anonymous buffer

        # prepare write payload once (reused for every full block)
        if op == "WRITE":
            if s.write_mode == "RANDOM":
                write_payload = os.urandom(bs)
            else:
                write_payload = b"\x00" * bs
            if want_direct:
                direct_buf.seek(0)
                direct_buf.write(write_payload)

        if op == "READ" and s.dump_path:
            try:
                dumpf = open(s.dump_path, "wb")
                s.add_log("info", "Dumping read data to %s" % s.dump_path)
            except OSError as e:
                s.add_log("error", "cannot open dump file: %s" % e)
                dumpf = None

        s.add_log("info", "Start %s on %s  [%s .. %s]  block=%s  direct=%s" % (
            op, s.disk.id_name, human_size(s.start), human_size(s.end),
            human_size(bs), "yes" if want_direct else "no"))

        offset = s.start
        idx = 0
        while offset < s.end and not s._stop.is_set():
            # pause loop
            while s._pause.is_set() and not s._stop.is_set():
                with s.lock:
                    s.status = "paused"
                time.sleep(0.1)
            if s._stop.is_set():
                break
            with s.lock:
                s.status = "running"
                s.cur_index = idx

            length = min(bs, s.end - offset)
            this_aligned = want_direct and (length % ALIGN == 0)
            is_error = False
            t0 = time.monotonic()
            try:
                if op in ("VERIFY", "READ"):
                    if this_aligned:
                        n = os.preadv(fdd, [direct_buf], offset)
                        data = direct_buf[:n] if dumpf else None
                    else:
                        data = os.pread(fdb, length, offset)
                        n = len(data)
                        os.posix_fadvise(fdb, offset, length, os.POSIX_FADV_DONTNEED)
                    if op == "READ" and dumpf and data:
                        dumpf.write(data)
                    if n < length:
                        # short read near a defect
                        is_error = True
                else:  # WRITE
                    if this_aligned:
                        n = os.pwritev(fdd, [direct_buf], offset)
                    else:
                        os.pwrite(fdb, write_payload[:length], offset)
                        os.posix_fadvise(fdb, offset, length, os.POSIX_FADV_DONTNEED)
            except OSError as e:
                is_error = True
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                name = errno.errorcode.get(e.errno, str(e.errno))
                s.add_log("error", "block %d @ %s : %s (%s)" % (
                    idx, human_size(offset), os.strerror(e.errno), name))
            elapsed_ms = (time.monotonic() - t0) * 1000.0

            ridx = p.classify(elapsed_ms, is_error)
            with s.lock:
                s.results[idx] = ridx
                s.counters[ridx] += 1
                s.processed += 1
                s.bytes_done += length
                s.io_seconds += elapsed_ms / 1000.0
                if is_error:
                    s.bad_offsets.append(offset)
            if ridx == p.timeout_index:
                s.add_log("warn", "slow block %d @ %s : %.0f ms (timeout bucket)" % (
                    idx, human_size(offset), elapsed_ms))

            offset += length
            idx += 1

        # finalize
        if dumpf:
            dumpf.flush()
            dumpf.close()
        if op == "WRITE":
            try:
                os.fsync(fdb)
            except OSError:
                pass
        with s.lock:
            if s._stop.is_set():
                s.status = "aborted"
                s.message = "Stopped by user"
            else:
                s.status = "done"
                s.message = "Completed"
        bad = len(s.bad_offsets)
        s.add_log("info", "%s — %d blocks, %d I/O errors, %s processed" % (
            s.status.upper(), s.processed, bad, human_size(s.bytes_done)))
    finally:
        for fd in (fdd, fdb):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if direct_buf is not None:
            try:
                direct_buf.close()
            except Exception:
                pass


# ===========================================================================
#  Curses UI
# ===========================================================================
COLOR_TEXT_BASE = 1     # text pairs start here
COLOR_BLOCK_BASE = 100  # block (bg-coloured) pairs start here


class UI:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.disks = []
        self.disk = None
        self.view = "PICKER"      # PICKER | SMART | CHECK
        self.smart = None
        self.smart_scroll = 0

        self.presets = discover_presets()
        self.preset_idx = 0
        self.preset = load_preset(self.presets[0]) if self.presets else _builtin_preset()

        # check panel fields
        self.fields = []          # filled in build_fields()
        self.field_idx = 0
        self.editing = False
        self.editbuf = ""

        self.session = None
        self.worker = None
        self.log_scroll = 0

        self.color_name_to_pair = {}   # text colour name -> pair id
        self.block_name_to_pair = {}   # block colour name -> pair id
        self.use256 = False

        self.params = {
            "start": 0,
            "end": 0,
            "block": 4 * 1024 * 1024,
            "op": "VERIFY",
            "write_mode": "ZERO",
            "dump": "",
            "direct": True,
            "grid_mode": "SCAN",
        }
        self.picker_idx = 0
        self.status_msg = ""

    # ---- colour setup -----------------------------------------------------
    def setup_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            default_bg = -1
        except Exception:
            default_bg = curses.COLOR_BLACK
        self.use256 = curses.COLORS >= 256

        def resolve(name):
            idx256, c8 = PALETTE.get(name, (15, curses.COLOR_WHITE))
            return idx256 if self.use256 else c8

        pid = COLOR_TEXT_BASE
        for name in PALETTE:
            curses.init_pair(pid, resolve(name), default_bg)
            self.color_name_to_pair[name] = pid
            pid += 1
        # a few fixed UI pairs
        self.PAIR_TAB_ACTIVE = pid
        curses.init_pair(pid, curses.COLOR_BLACK, resolve("cyan")); pid += 1
        self.PAIR_WARN = pid
        curses.init_pair(pid, curses.COLOR_BLACK, resolve("yellow")); pid += 1
        self.PAIR_DANGER = pid
        curses.init_pair(pid, resolve("white"), resolve("dark_red")); pid += 1
        self.PAIR_SEL = pid
        curses.init_pair(pid, curses.COLOR_BLACK, resolve("white")); pid += 1

        # block (background-coloured) pairs
        bid = COLOR_BLOCK_BASE
        for name in PALETTE:
            curses.init_pair(bid, curses.COLOR_BLACK, resolve(name))
            self.block_name_to_pair[name] = bid
            bid += 1
        # error block: white fg on its bg
        self.PAIR_BLOCK_ERROR = bid
        fg = resolve(self.preset.error_color)
        bg = resolve(self.preset.error_bg)
        curses.init_pair(bid, fg, bg); bid += 1

    def text_attr(self, name):
        return curses.color_pair(self.color_name_to_pair.get(name, COLOR_TEXT_BASE))

    def block_attr_for_index(self, ridx):
        if ridx == self.preset.error_index:
            return curses.color_pair(self.PAIR_BLOCK_ERROR)
        name = self.preset.color_name(ridx)
        return curses.color_pair(self.block_name_to_pair.get(name, COLOR_BLOCK_BASE))

    # ---- safe drawing -----------------------------------------------------
    def addstr(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    # ---- top / bottom bars ------------------------------------------------
    def draw_tabs(self):
        h, w = self.scr.getmaxyx()
        self.addstr(0, 0, " " * (w - 1), curses.A_REVERSE)
        title = " diskrefresh "
        self.addstr(0, 0, title, curses.A_REVERSE | curses.A_BOLD)
        tabs = [("SMART", "SMART"), ("Check & Repair", "CHECK")]
        x = len(title) + 2
        for label, view in tabs:
            attr = curses.color_pair(self.PAIR_TAB_ACTIVE) | curses.A_BOLD if self.view == view \
                else curses.A_REVERSE
            self.addstr(0, x, " %s " % label, attr)
            x += len(label) + 3
        if self.disk:
            info = "%s  %s  %s" % (self.disk.id_name, self.disk.model, human_size(self.disk.size))
            self.addstr(0, max(x + 2, w - len(info) - 2), info, curses.A_REVERSE)

    def draw_footer(self, keys):
        h, w = self.scr.getmaxyx()
        self.addstr(h - 1, 0, " " * (w - 1), curses.A_REVERSE)
        self.addstr(h - 1, 0, keys, curses.A_REVERSE)
        if self.status_msg:
            self.addstr(h - 1, max(0, w - len(self.status_msg) - 2),
                        self.status_msg, curses.A_REVERSE | curses.A_BOLD)

    # ---- device picker ----------------------------------------------------
    def draw_picker(self):
        self.scr.erase()
        self.draw_tabs()
        self.addstr(2, 2, "Select a disk (identified via /dev/disk/by-id/):", curses.A_BOLD)
        if not self.disks:
            self.addstr(4, 4, "No disks found. Are you root? Is /dev/disk/by-id present?",
                        self.text_attr("red"))
        for i, d in enumerate(self.disks):
            y = 4 + i * 2
            sel = (i == self.picker_idx)
            attr = curses.color_pair(self.PAIR_SEL) if sel else 0
            line = "%s  %-24s  %s" % ("➤" if sel else " ", d.model[:24], human_size(d.size))
            self.addstr(y, 4, line, attr | (curses.A_BOLD if sel else 0))
            self.addstr(y + 1, 7, "id: %s   (-> %s)" % (d.id_name, d.real),
                        self.text_attr("grey"))
        self.draw_footer(" ↑/↓ select   Enter open   r rescan   q quit ")
        self.scr.noutrefresh()
        curses.doupdate()

    # ---- SMART view -------------------------------------------------------
    def draw_smart(self):
        self.scr.erase()
        self.draw_tabs()
        h, w = self.scr.getmaxyx()
        if self.smart is None:
            self.addstr(2, 2, "Press 'r' to load SMART data…", self.text_attr("grey"))
            self.draw_footer(" ←/→/Tab switch tab   r refresh   d device   q quit ")
            self.scr.noutrefresh(); curses.doupdate(); return
        s = self.smart
        y = 2
        if s.error:
            self.addstr(y, 2, s.error, self.text_attr("red")); y += 2
        # summary box
        hcol = "green" if "PASS" in s.health.upper() else ("red" if s.health != "?" else "grey")
        self.addstr(y, 2, "Health: ", curses.A_BOLD)
        self.addstr(y, 10, s.health, self.text_attr(hcol) | curses.A_BOLD); y += 1
        for label, val in (("Model", s.model), ("Serial", s.serial),
                           ("Firmware", s.firmware), ("Capacity", s.capacity),
                           ("Rotation", s.rotation), ("Temperature", s.temperature),
                           ("Power-on", s.power_on)):
            if val:
                self.addstr(y, 2, "%-12s %s" % (label + ":", val)); y += 1
        if s.criticals:
            y += 1
            self.addstr(y, 2, "Critical attributes:", curses.A_BOLD | curses.A_UNDERLINE); y += 1
            for name, raw, bad in s.criticals:
                col = "red" if bad else "green"
                self.addstr(y, 4, "%-26s %s" % (name, raw), self.text_attr(col)); y += 1
        # raw scrollable output
        y += 1
        self.addstr(y, 2, "── full smartctl -a (PgUp/PgDn to scroll) ──", self.text_attr("grey"))
        y += 1
        lines = s.raw_text.splitlines()
        avail = h - y - 1
        self.smart_scroll = max(0, min(self.smart_scroll, max(0, len(lines) - avail)))
        for i in range(avail):
            li = self.smart_scroll + i
            if li >= len(lines):
                break
            self.addstr(y + i, 2, lines[li][: w - 4])
        self.draw_footer(" ←/→/Tab tab   r refresh   PgUp/PgDn/↑/↓ scroll   d device   q quit ")
        self.scr.noutrefresh(); curses.doupdate()

    # ---- CHECK view -------------------------------------------------------
    def build_fields(self):
        p = self.params
        running = self.session is not None and self.session.status in ("running", "paused")
        op = p["op"]
        fields = [
            ("preset", "Preset", self.preset.name),
            ("start", "Range start", "%d  (%s)" % (p["start"], human_size(p["start"]))),
            ("end", "Range end", "%d  (%s)" % (p["end"], human_size(p["end"]))),
            ("block", "Block size", "%s" % human_size(p["block"])),
            ("op", "Operation", op),
        ]
        if op == "WRITE":
            fields.append(("write_mode", "Write fill", p["write_mode"]))
        if op == "READ":
            fields.append(("dump", "Dump file", p["dump"] or "(none — verify only)"))
        fields.append(("direct", "Direct I/O", "ON" if p["direct"] else "OFF"))
        fields.append(("grid_mode", "Grid view",
                       "SCAN (1 cell=1 block)" if p["grid_mode"] == "SCAN"
                       else "MAP (whole disk)"))
        fields.append(("start_btn", "", "▶ START" if not running else "⏸ running…"))
        self.fields = fields
        if self.field_idx >= len(self.fields):
            self.field_idx = len(self.fields) - 1

    def draw_check(self):
        self.scr.erase()
        self.draw_tabs()
        h, w = self.scr.getmaxyx()
        if self.disk is None:
            self.addstr(2, 2, "No disk selected — press 'd'.", self.text_attr("red"))
            self.draw_footer(" d device   ←/→/Tab tab   q quit ")
            self.scr.noutrefresh(); curses.doupdate(); return

        self.build_fields()

        panel_w = 45
        log_h = min(7, h // 3)
        grid_x0 = 1
        grid_w = w - panel_w - 3
        cols = max(1, grid_w // 2)   # each cell is 2 chars wide
        # row 1 = progress bar, grid starts at row 2
        bar_y = 1
        grid_y0 = 2
        grid_h = h - log_h - 4
        rows = max(1, grid_h)

        # stash geometry so the panel can explain the aggregation
        s = self.session
        total = s.total_blocks if s is not None else self.params_total_blocks()
        ncells = rows * cols
        self._grid_cells = ncells
        self._grid_dims = (cols, rows)
        self._per_cell = max(1, (total + ncells - 1) // ncells)
        block = s.block_size if s is not None else self.params["block"]
        self._bytes_per_cell = self._per_cell * block

        # --- progress bar (smooth feedback even when a cell hasn't flipped) ---
        self.draw_progress_bar(grid_x0, bar_y, grid_w)
        # --- grid ---
        self.draw_grid(grid_x0, grid_y0, rows, cols)
        # --- right panel ---
        self.draw_panel(w - panel_w - 1, 1, panel_w, h - log_h - 3)
        # --- log ---
        self.draw_log(1, h - log_h - 1, w - 2, log_h)

        if self.session and self.session.status in ("running", "paused"):
            foot = " Space pause/resume   s stop   ←/→/Tab tab   q quit "
        elif self.editing:
            foot = " type value   Enter commit   Esc cancel "
        else:
            foot = " ↑/↓ field   -/+ change   Enter edit/START   ←/→/Tab tab   d device   q quit "
        self.draw_footer(foot)
        self.scr.noutrefresh(); curses.doupdate()

    def draw_progress_bar(self, x0, y, width):
        # Built from background-coloured spaces only (no glyphs, locale-proof).
        s = self.session
        if width < 12:
            return
        if s is None:
            frac, status = 0.0, "idle"
        else:
            with s.lock:
                processed, total = s.processed, s.total_blocks
                status = s.status
            frac = (processed / total) if total else 0.0
        label = "%5.1f%% " % (frac * 100)
        self.addstr(y, x0, label, self.text_attr("cyan") | curses.A_BOLD)
        bx = x0 + len(label)
        bw = max(1, width - len(label))
        filled = int(frac * bw)
        fillcol = "green" if status in ("running", "done") else (
            "yellow" if status == "paused" else "red" if status == "error" else "grey")
        self.addstr(y, bx, " " * filled,
                    curses.color_pair(self.block_name_to_pair[fillcol]))
        self.addstr(y, bx + filled, " " * (bw - filled),
                    curses.color_pair(self.block_name_to_pair["grey"]))

    def draw_grid(self, x0, y0, rows, cols):
        # Cells are painted with two SPACES using a background-coloured pair, so
        # the bucket colour is the cell BACKGROUND. (Full-block glyphs would be
        # drawn in the *foreground* colour and also depend on a UTF-8 locale,
        # which is exactly what used to leave the grid blank/grey.)
        s = self.session
        ncells = rows * cols
        grey = curses.color_pair(self.block_name_to_pair["grey"])
        if s is None:
            for r in range(rows):
                self.addstr(y0 + r, x0, "  " * cols, grey)
            self.addstr(y0 + rows // 2, x0 + max(0, cols - 7), " idle ",
                        self.text_attr("grey"))
            return
        with s.lock:
            total = s.total_blocks
            results = s.results          # reference; byte reads are atomic enough for display
            cur = s.cur_index
            status = s.status
        terr = self.preset.error_index
        mode = self.params.get("grid_mode", "SCAN")

        if mode == "SCAN":
            # 1 cell == 1 block. Show the page that contains the leading block,
            # filling left->right, top->bottom; flip to the next page when full.
            lead = min(cur, total - 1)
            total_pages = max(1, (total + ncells - 1) // ncells)
            page = min(lead // ncells, total_pages - 1)
            base = page * ncells
            self._grid_page = (page + 1, total_pages, base,
                               min(total, base + ncells) - 1)
            for c in range(ncells):
                r = c // cols
                cc = c % cols
                blk = base + c
                if blk >= total:
                    attr = grey
                else:
                    v = results[blk]
                    if v == UNPROCESSED:
                        attr = grey
                    else:
                        attr = self.block_attr_for_index(v if v <= terr else terr)
                    if blk == lead and status == "running":
                        attr |= curses.A_REVERSE
                self.addstr(y0 + r, x0 + cc * 2, "  ", attr)
            return

        # MAP mode: whole disk as a proportional heat-map (1 cell ~ many blocks).
        self._grid_page = None
        per_cell = max(1, (total + ncells - 1) // ncells)
        for c in range(ncells):
            r = c // cols
            cc = c % cols
            b0 = c * per_cell
            if b0 >= total:
                attr = grey                      # cells past end of range -> grey
            else:
                b1 = min(total, b0 + per_cell)
                worst = -1
                for b in range(b0, b1):
                    v = results[b]
                    if v == UNPROCESSED:
                        continue
                    sev = v if v <= terr else 0
                    if sev > worst:
                        worst = sev
                if worst < 0:
                    attr = grey                  # in range but not processed yet
                else:
                    attr = self.block_attr_for_index(worst)
                    if b0 <= cur < b1 and status == "running":
                        attr |= curses.A_REVERSE  # moving marker on the leading edge
            self.addstr(y0 + r, x0 + cc * 2, "  ", attr)


    def draw_panel(self, x0, y0, width, height):
        s = self.session
        y = y0
        self.addstr(y, x0, "─ Controls ".ljust(width, "─"), self.text_attr("cyan")); y += 1
        for i, (key, label, val) in enumerate(self.fields):
            sel = (i == self.field_idx)
            if key == "start_btn":
                y += 1
                running = s is not None and s.status in ("running", "paused")
                attr = curses.color_pair(self.PAIR_DANGER if self.params["op"] == "WRITE"
                                         else self.PAIR_TAB_ACTIVE)
                if sel:
                    attr |= curses.A_BOLD | curses.A_REVERSE
                self.addstr(y, x0, (" %s " % val).center(width - 1), attr)
                y += 1
                continue
            marker = "➤" if sel else " "
            self.addstr(y, x0, "%s %-11s" % (marker, label),
                        (curses.A_BOLD if sel else 0))
            shown = val
            if self.editing and sel:
                shown = self.editbuf + "▏"
                self.addstr(y, x0 + 14, shown[: width - 15], self.text_attr("yellow"))
            else:
                self.addstr(y, x0 + 14, shown[: width - 15],
                            self.text_attr("cyan") if sel else 0)
            y += 1

        # counters
        y += 1
        self.addstr(y, x0, "─ Counters ".ljust(width, "─"), self.text_attr("cyan")); y += 1
        labels = self.preset.labels
        if s is not None:
            counts, processed, total = s.snapshot_counts()
        else:
            counts = [0] * (self.preset.error_index + 1)
            processed, total = 0, self.params_total_blocks()
        for idx, lab in enumerate(labels):
            attr = self.block_attr_for_index(idx)
            self.addstr(y, x0, "  ", attr)
            self.addstr(y, x0 + 3, "%-12s %8d" % (lab, counts[idx]))
            y += 1
        # grid resolution (makes the heatmap aggregation transparent)
        y += 1
        cols, rows = getattr(self, "_grid_dims", (0, 0))
        per_cell = getattr(self, "_per_cell", 1)
        bpc = getattr(self, "_bytes_per_cell", 0)
        mode = self.params.get("grid_mode", "SCAN")
        page = getattr(self, "_grid_page", None)
        self.addstr(y, x0, "─ Grid ".ljust(width, "─"), self.text_attr("cyan")); y += 1
        self.addstr(y, x0, "%d×%d = %d cells" % (cols, rows, cols * rows)); y += 1
        if mode == "SCAN":
            self.addstr(y, x0, "view: SCAN  1 cell=1 block", self.text_attr("green")); y += 1
            if page:
                self.addstr(y, x0, "page %d/%d  blk %d–%d"
                            % (page[0], page[1], page[2], page[3])); y += 1
        else:
            self.addstr(y, x0, "view: MAP (whole disk)", self.text_attr("cyan")); y += 1
            if per_cell <= 1:
                self.addstr(y, x0, "1 cell = 1 block (1:1)", self.text_attr("green")); y += 1
            else:
                self.addstr(y, x0, "1 cell ≈ %d blocks (%s)" % (per_cell, human_size(bpc)),
                            self.text_attr("grey")); y += 1
        self.addstr(y, x0, "press 'm' to switch view", self.text_attr("grey")); y += 1

        # progress / throughput
        y += 1
        pct = (processed / total * 100.0) if total else 0
        self.addstr(y, x0, "Progress: %6.2f%%" % pct); y += 1
        self.addstr(y, x0, "Blocks:   %d / %d" % (processed, total), ); y += 1
        if s is not None:
            with s.lock:
                bd, secs = s.bytes_done, s.io_seconds
                elapsed = time.time() - s.started_at
                st = s.status
            mbps = (bd / secs / 1e6) if secs > 0 else 0
            self.addstr(y, x0, "Speed:    %7.1f MB/s" % mbps); y += 1
            self.addstr(y, x0, "Elapsed:  %s" % _fmt_dur(elapsed)); y += 1
            # ETA
            if processed and st == "running":
                rate = processed / max(0.001, elapsed)
                eta = (total - processed) / rate if rate else 0
                self.addstr(y, x0, "ETA:      %s" % _fmt_dur(eta)); y += 1
            scol = {"running": "green", "paused": "yellow", "done": "green",
                    "error": "red", "aborted": "yellow"}.get(st, "grey")
            self.addstr(y, x0, "Status:   ")
            self.addstr(y, x0 + 10, st.upper(), self.text_attr(scol) | curses.A_BOLD)

    def draw_log(self, x0, y0, width, height):
        self.addstr(y0, x0, "─ Log ".ljust(width, "─"), self.text_attr("cyan"))
        s = self.session
        if s is None:
            return
        with s.lock:
            entries = list(s.log)
        avail = height - 1
        view = entries[-avail:]
        for i, (ts, level, msg) in enumerate(view):
            col = {"error": "red", "warn": "yellow", "info": "grey"}.get(level, "white")
            line = "%s [%s] %s" % (ts, level.upper(), msg)
            self.addstr(y0 + 1 + i, x0, line[: width], self.text_attr(col))

    # ---- helpers ----------------------------------------------------------
    def params_total_blocks(self):
        p = self.params
        if p["end"] <= p["start"]:
            return 1
        return max(1, (p["end"] - p["start"] + p["block"] - 1) // p["block"])

    # ---- event handling ---------------------------------------------------
    def load_disks(self):
        self.disks = discover_disks()
        if self.picker_idx >= len(self.disks):
            self.picker_idx = max(0, len(self.disks) - 1)

    def select_disk(self, d):
        self.disk = d
        self.params["start"] = 0
        self.params["end"] = d.size
        self.smart = None
        self.smart_scroll = 0
        self.view = "SMART"

    def cycle_op(self, direction):
        order = ["VERIFY", "READ", "WRITE"]
        i = order.index(self.params["op"])
        self.params["op"] = order[(i + direction) % len(order)]

    def cycle_field(self, key, direction):
        p = self.params
        if key == "op":
            self.cycle_op(direction)
        elif key == "write_mode":
            p["write_mode"] = "RANDOM" if p["write_mode"] == "ZERO" else "ZERO"
        elif key == "direct":
            p["direct"] = not p["direct"]
        elif key == "grid_mode":
            p["grid_mode"] = "MAP" if p["grid_mode"] == "SCAN" else "SCAN"
        elif key == "preset" and self.presets:
            self.preset_idx = (self.preset_idx + direction) % len(self.presets)
            self.preset = load_preset(self.presets[self.preset_idx])
            self.setup_colors()  # error-pair fg/bg may differ
            self.status_msg = "preset: %s" % self.preset.name
        elif key == "block":
            steps = [4096, 65536, 1024*1024, 4*1024*1024, 16*1024*1024, 64*1024*1024]
            cur = p["block"]
            # find nearest then move
            nearest = min(range(len(steps)), key=lambda i: abs(steps[i] - cur))
            p["block"] = steps[min(len(steps)-1, max(0, nearest + direction))]

    def begin_edit(self, key):
        p = self.params
        self.editing = True
        if key == "start":
            self.editbuf = str(p["start"])
        elif key == "end":
            self.editbuf = str(p["end"])
        elif key == "block":
            self.editbuf = human_size(p["block"]).replace(" ", "")
        elif key == "dump":
            self.editbuf = p["dump"]
        else:
            self.editing = False

    def commit_edit(self, key):
        p = self.params
        try:
            if key in ("start", "end", "block"):
                v = parse_size(self.editbuf)
                if key == "block":
                    if v < 512:
                        raise ValueError("block too small")
                    p["block"] = v
                else:
                    p[key] = max(0, v)
                    if p["end"] and p["end"] <= p["start"]:
                        self.status_msg = "warning: end <= start"
            elif key == "dump":
                p["dump"] = self.editbuf.strip()
        except ValueError as e:
            self.status_msg = "bad value: %s" % e
        self.editing = False

    def start_session(self):
        p = self.params
        if p["end"] <= p["start"]:
            self.status_msg = "range invalid (end must be > start)"
            return
        if p["end"] > self.disk.size:
            p["end"] = self.disk.size
        # safety: mounted?
        mp = mounted_partitions(self.disk)
        if p["op"] == "WRITE" and mp:
            ok = self.confirm_modal(
                "DISK IS MOUNTED",
                ["The following partitions of this disk are mounted:"] +
                ["   " + m for m in mp] +
                ["", "Writing will corrupt live filesystems.",
                 "Unmount them first. Proceeding is strongly discouraged."],
                require=self.disk.id_name, danger=True)
            if not ok:
                return
        elif p["op"] == "WRITE":
            ok = self.confirm_modal(
                "DESTRUCTIVE WRITE",
                ["You are about to OVERWRITE data on:",
                 "   model : %s" % self.disk.model,
                 "   id    : %s" % self.disk.id_name,
                 "   dev   : %s" % self.disk.real,
                 "   size  : %s" % human_size(self.disk.size),
                 "   range : %s .. %s" % (human_size(p["start"]), human_size(p["end"])),
                 "   fill  : %s" % p["write_mode"],
                 "",
                 "This CANNOT be undone (MBR/EFI/data will be erased).",
                 "Type the disk id below to confirm:"],
                require=self.disk.id_name, danger=True)
            if not ok:
                return
        # build & start session
        self.session = Session(self.disk, p["start"], p["end"], p["block"],
                               p["op"], p["write_mode"], p["dump"], p["direct"],
                               self.preset)
        self.log_scroll = 0
        self.worker = threading.Thread(target=io_worker, args=(self.session,), daemon=True)
        self.worker.start()
        self.status_msg = "%s started" % p["op"]
        # one-time hint so the grid behaviour is clear
        ncells = max(1, getattr(self, "_grid_cells", 1))
        if self.session.total_blocks > ncells:
            per_cell = (self.session.total_blocks + ncells - 1) // ncells
            rec = (p["end"] - p["start"] + ncells - 1) // ncells
            if p["grid_mode"] == "SCAN":
                self.session.add_log(
                    "info", "SCAN view: 1 cell = 1 block.")
            else:
                self.session.add_log(
                    "info", "MAP view: 1 cell ≈ %d blocks; colour fills in proportion to %% done. Press 'm' for SCAN (1 cell = 1 block), or use block size ≥ %s for a 1:1 map." % (per_cell, human_size(rec)))

    def confirm_modal(self, title, lines, require=None, danger=False):
        """Block until user confirms. If `require` set, user must type it exactly."""
        curses.curs_set(1 if require else 0)
        buf = ""
        while True:
            h, w = self.scr.getmaxyx()
            bw = min(w - 4, max(50, max(len(x) for x in lines + [title]) + 6))
            bh = len(lines) + (4 if require else 3)
            y0 = max(1, (h - bh) // 2)
            x0 = max(1, (w - bw) // 2)
            attr = curses.color_pair(self.PAIR_DANGER if danger else self.PAIR_WARN)
            for r in range(bh):
                self.addstr(y0 + r, x0, " " * bw, attr)
            self.addstr(y0, x0, " " + title.center(bw - 2), attr | curses.A_BOLD)
            for i, ln in enumerate(lines):
                self.addstr(y0 + 2 + i, x0 + 2, ln[: bw - 4], attr)
            if require:
                self.addstr(y0 + bh - 2, x0 + 2, "> " + buf + "▏", attr | curses.A_BOLD)
                hint = "Enter=confirm  Esc=cancel"
            else:
                hint = "Enter/y=yes   Esc/n=no"
            self.addstr(y0 + bh - 1, x0 + 2, hint, attr)
            self.scr.noutrefresh(); curses.doupdate()
            ch = self.scr.getch()
            if ch in (27,):  # Esc
                curses.curs_set(0); return False
            if require:
                if ch in (curses.KEY_ENTER, 10, 13):
                    curses.curs_set(0)
                    if buf.strip() == require:
                        return True
                    self.status_msg = "confirmation text did not match"
                    return False
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    buf = buf[:-1]
                elif 32 <= ch < 127:
                    buf += chr(ch)
            else:
                if ch in (ord('y'), ord('Y'), curses.KEY_ENTER, 10, 13):
                    curses.curs_set(0); return True
                if ch in (ord('n'), ord('N')):
                    curses.curs_set(0); return False

    # ---- main loop --------------------------------------------------------
    def loop(self):
        self.scr.nodelay(True)
        curses.curs_set(0)
        self.load_disks()
        last_draw = 0
        while True:
            now = time.time()
            # redraw at ~12 fps or on key
            if now - last_draw > 0.08:
                self.render()
                last_draw = now
            ch = self.scr.getch()
            if ch == -1:
                time.sleep(0.02)
                continue
            if self.handle_key(ch):
                return  # quit

    def render(self):
        try:
            if self.view == "PICKER":
                self.draw_picker()
            elif self.view == "SMART":
                self.draw_smart()
            else:
                self.draw_check()
        except curses.error:
            pass

    def handle_key(self, ch):
        # global
        if self.view == "PICKER":
            return self.key_picker(ch)
        if ch == 9:  # Tab
            self.view = "CHECK" if self.view == "SMART" else "SMART"
            return False
        # Left/Right also switch tabs (only two tabs, so both just toggle).
        # Guarded by `not editing` so they don't fire while typing a field value.
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and not self.editing:
            self.view = "CHECK" if self.view == "SMART" else "SMART"
            return False
        if ch in (ord('d'), ord('D')) and not self.editing:
            self.load_disks(); self.view = "PICKER"; return False
        if self.view == "SMART":
            return self.key_smart(ch)
        return self.key_check(ch)

    def key_picker(self, ch):
        if ch in (ord('q'), ord('Q')):
            return True
        if ch in (curses.KEY_UP, ord('k')):
            self.picker_idx = max(0, self.picker_idx - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            self.picker_idx = min(len(self.disks) - 1, self.picker_idx + 1)
        elif ch in (ord('r'), ord('R')):
            self.load_disks()
        elif ch in (curses.KEY_ENTER, 10, 13):
            if self.disks:
                self.select_disk(self.disks[self.picker_idx])
        return False

    def key_smart(self, ch):
        if ch in (ord('q'), ord('Q')):
            return True
        if ch in (ord('r'), ord('R')):
            self.status_msg = "reading SMART…"
            self.render()
            self.smart = read_smart(self.disk)
            self.status_msg = ""
        elif ch in (curses.KEY_NPAGE,):
            self.smart_scroll += 10
        elif ch in (curses.KEY_PPAGE,):
            self.smart_scroll = max(0, self.smart_scroll - 10)
        elif ch in (curses.KEY_DOWN,):
            self.smart_scroll += 1
        elif ch in (curses.KEY_UP,):
            self.smart_scroll = max(0, self.smart_scroll - 1)
        return False

    def key_check(self, ch):
        s = self.session
        running = s is not None and s.status in ("running", "paused")
        if self.editing:
            key = self.fields[self.field_idx][0]
            if ch in (curses.KEY_ENTER, 10, 13):
                self.commit_edit(key)
            elif ch == 27:
                self.editing = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.editbuf = self.editbuf[:-1]
            elif 32 <= ch < 127:
                self.editbuf += chr(ch)
            return False

        if running:
            if ch == ord(' '):
                if s.is_paused():
                    s.resume()
                else:
                    s.pause()
            elif ch in (ord('s'), ord('S')):
                s.request_stop()
                self.status_msg = "stopping…"
            elif ch in (ord('m'), ord('M')):
                self.cycle_field("grid_mode", +1)
            elif ch in (ord('q'), ord('Q')):
                s.request_stop()
            return False

        if ch in (ord('q'), ord('Q')):
            return True
        if ch in (curses.KEY_UP, ord('k')):
            self.field_idx = max(0, self.field_idx - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            self.field_idx = min(len(self.fields) - 1, self.field_idx + 1)
        elif ch in (ord('-'), ord('_')):
            self.cycle_field(self.fields[self.field_idx][0], -1)
        elif ch in (ord('+'), ord('=')):
            self.cycle_field(self.fields[self.field_idx][0], +1)
        elif ch in (ord('m'), ord('M')):
            self.cycle_field("grid_mode", +1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            key = self.fields[self.field_idx][0]
            if key == "start_btn":
                self.start_session()
            elif key in ("op", "write_mode", "direct", "preset", "grid_mode"):
                self.cycle_field(key, +1)
            else:
                self.begin_edit(key)
        return False


def _fmt_dur(sec):
    sec = int(sec)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if h:
        return "%dh%02dm%02ds" % (h, m, s)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def _builtin_preset():
    p = Preset("Built-in default", "<builtin>")
    p.buckets = [(100, "green"), (200, "light_green"), (500, "yellow"),
                 (1000, "orange"), (5000, "red")]
    p.timeout_color = "magenta"
    p.error_color = "white"
    p.error_bg = "dark_red"
    return p


def main(stdscr):
    ui = UI(stdscr)
    ui.setup_colors()
    ui.loop()


def cli():
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if os.geteuid() != 0:
        sys.stderr.write(
            "\n[!] Not running as root. Raw device access will fail.\n"
            "    Re-run with: sudo python3 %s\n\n" % os.path.basename(__file__))
        # continue anyway so the picker can at least be shown
        time.sleep(1.5)
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
