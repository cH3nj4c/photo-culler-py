"""Resource-sampling verification.

Split deliberately: the formatting and clamping rules are pure and always run,
while the PDH / process-counter checks depend on the host having GPU
performance counters. When they do not (a VM, a bare CI box), those steps say
so and continue rather than failing — the sidebar is designed to degrade to
"—" in exactly that situation, so asserting the values exist would be asserting
the wrong contract.

Run:  python test_sysmon.py
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sysmon  # noqa: E402

# --- 1. formatting (pure) ----------------------------------------------------
assert sysmon.format_mb(None) == "—"
assert sysmon.format_mb(0) == "0 MB"
assert sysmon.format_mb(512) == "512 MB"
assert sysmon.format_mb(1023) == "1023 MB"
assert sysmon.format_mb(1024) == "1.0 GB"
assert sysmon.format_mb(1536) == "1.5 GB"
assert sysmon.format_mb(16159) == "15.8 GB"
assert sysmon.format_percent(None) == "—"
assert sysmon.format_percent(0) == "0%"
assert sysmon.format_percent(33.3) == "33%"
print("[1] format_mb / format_percent OK")

# --- 2. usage strings degrade instead of lying -------------------------------
# A missing measurement must read "—", never "0" — the two look identical to a
# user but mean opposite things.
assert sysmon._format_usage(None, 100, None) == "—"
assert sysmon._format_usage(2048, None, None) == "2.0 GB", sysmon._format_usage(2048, None, None)
assert sysmon._format_usage(512, 0, None) == "512 MB"
# Shared unit is factored out so the narrow sidebar does not crowd the label.
assert sysmon._format_usage(11686, 16159, 72.3) == "11.4/15.8 GB 72%"
assert sysmon._format_usage(1179, 6141, 19.2) == "1.2/6.0 GB 19%"
# Below 1 GB on either side, units must stay explicit or it reads as nonsense.
assert sysmon._format_usage(300, 900, 33.0) == "300 MB/900 MB 33%"
assert sysmon._format_usage(1179, 6141, None) == "1.2/6.0 GB"
print("[2] usage strings factor units and degrade to — rather than 0")

# --- 3. percentages are clamped ---------------------------------------------
over = sysmon.Snapshot(ram_total_mb=100, ram_used_mb=250)
assert over.ram_percent == 100.0, over.ram_percent
assert sysmon.Snapshot(ram_total_mb=0, ram_used_mb=10).ram_percent == 0.0
half = sysmon.Snapshot(ram_total_mb=200, ram_used_mb=50)
assert half.ram_percent == 25.0
assert sysmon.Snapshot(vram_total_mb=100, vram_used_mb=250).vram_percent == 100.0
# No denominator means unknown, not 0%.
assert sysmon.Snapshot(vram_used_mb=100).vram_percent is None
assert sysmon.Snapshot(vram_total_mb=0, vram_used_mb=100).vram_percent is None
assert not sysmon.Snapshot().has_gpu
assert sysmon.Snapshot(gpu_percent=0.0).has_gpu, "0% is a measurement, not missing"
print("[3] percentages clamp at 100 and missing denominators stay None")

# --- 4. the sidebar rows -----------------------------------------------------
live = sysmon.Snapshot(
    ram_total_mb=16159, ram_used_mb=11686, app_ram_mb=221,
    gpu_percent=5.16, app_gpu_percent=0.0,
    vram_total_mb=6141, vram_used_mb=1179, app_vram_mb=21,
    adapters=((0x114AD, 5.16), (0x126C1, 0.0)),
)
rows = sysmon.resource_rows(live)
assert [title for title, _ in rows] == ["内存", "本程序", "GPU", "显存"], rows
assert dict(rows)["内存"] == "11.4/15.8 GB 72%", rows
assert dict(rows)["本程序"] == "221 MB", rows
assert dict(rows)["GPU"] == "5%", rows
assert dict(rows)["显存"] == "1.2/6.0 GB 19%", rows
# A machine without the counters still yields all four rows, with em dashes.
degraded = sysmon.Snapshot(ram_total_mb=16000, ram_used_mb=8000, app_ram_mb=20)
brow = dict(sysmon.resource_rows(degraded))
assert brow["GPU"] == "—" and brow["显存"] == "—", brow
assert brow["本程序"] == "20 MB" and brow["内存"].endswith("50%"), brow
print("[4] four rows, correct labels, graceful em dashes when unavailable")

# --- 5. tooltip resolves adapter names ---------------------------------------
tip = sysmon.resource_tooltip(live, {0x114AD: "NVIDIA GeForce RTX 4050 Laptop GPU"})
assert "11,686 / 16,159 MB" in tip, tip
assert "NVIDIA GeForce RTX 4050 Laptop GPU" in tip, tip
assert "专用显存" in tip, tip
plain = sysmon.resource_tooltip(sysmon.Snapshot(ram_total_mb=1, ram_used_mb=1))
assert "不可用" in plain, plain
# An unmapped LUID falls back to hex rather than vanishing.
unknown = sysmon.Snapshot(gpu_percent=3.0, adapters=((0xABCD, 3.0),))
tip2 = sysmon.resource_tooltip(unknown, {})
assert "0x000000000000ABCD" in tip2, tip2
print("[5] tooltip names adapters by LUID and falls back to hex")

# --- 6. LUID parsing ---------------------------------------------------------
inst = "pid_1234_luid_0x00000000_0x000114AD_phys_0_eng_0_engtype_3D"
assert sysmon._parse_luid(inst) == 0x114AD, hex(sysmon._parse_luid(inst))
# High half must land in the upper 32 bits, and a large LUID must stay positive.
assert sysmon._parse_luid("pid_1_luid_0x0000000A_0x0000000B_phys_0") == (0xA << 32) | 0xB
big = sysmon._parse_luid("pid_1_luid_0xFFFFFFFF_0xFFFFFFFF_phys_0")
assert big == 0xFFFFFFFFFFFFFFFF, hex(big)
assert sysmon._parse_luid("") is None and sysmon._parse_luid("pid_9_phys_0") is None
assert sysmon._PID_RE.match(inst).group(1) == "1234"
print("[6] LUID and pid parsing OK")

# --- 7. this process's working set -------------------------------------------
app_mb = sysmon.app_working_set_mb()
assert app_mb > 0, f"working set probe returned {app_mb}"
assert app_mb < 64 * 1024, f"implausible working set: {app_mb} MB"
print(f"[7] app working set = {app_mb} MB (GetCurrentProcess needs HANDLE restype)")

# --- 8. PDH counters (host dependent) ----------------------------------------
counters = sysmon.PdhGpuCounters()
opened = counters.open()
try:
    if not opened:
        print(f"[8] PDH unavailable here ({counters.reason}); degrade path exercised")
        snap = sysmon.sample_once(None, 6141)
        assert snap.gpu_percent is None and snap.vram_used_mb is None
        assert dict(sysmon.resource_rows(snap))["GPU"] == "—"
    else:
        assert counters.collect(), "first (priming) collect failed"
        time.sleep(1.1)
        assert counters.collect(), "second collect failed"
        percent, per_luid = counters.gpu_percent()
        assert 0.0 <= percent <= 100.0, percent
        assert all(0.0 <= v <= 100.0 for _, v in per_luid), per_luid
        # Per-adapter figures must not exceed the capped machine-wide total.
        assert all(v <= percent + 0.001 for _, v in per_luid) or not per_luid
        mine, _ = counters.gpu_percent(pid=os.getpid())
        assert 0.0 <= mine <= 100.0, mine
        adapter_bytes = counters.dedicated_bytes("adapter_mem")
        assert adapter_bytes >= 0
        app_bytes = counters.dedicated_bytes("proc_mem", pid=os.getpid())
        assert app_bytes >= 0
        snap = sysmon.sample_once(counters, 6141)
        assert snap.gpu_percent is not None and snap.vram_used_mb is not None
        assert snap.vram_used_mb <= 6141 * 4, f"implausible VRAM: {snap.vram_used_mb}"
        assert snap.ram_total_mb > 0 and snap.ram_used_mb >= 0
        assert 0.0 <= snap.ram_percent <= 100.0
        named = counters.gpu_percent()[1]
        print(f"[8] PDH OK: gpu={percent:.1f}% over {len(named)} adapter(s), "
              f"vram={snap.vram_used_mb} MB, ram={snap.ram_percent:.0f}%")
finally:
    counters.close()

# --- 9. the monitor thread ---------------------------------------------------
monitor = sysmon.SystemMonitor(interval_s=0.4, vram_total_mb=6141)
# A complete snapshot must exist before the thread has produced anything, so
# the panel can render on its very first paint.
assert monitor.latest().ram_total_mb > 0
monitor.start()
assert monitor._thread is not None and monitor._thread.daemon, "must not block exit"
assert monitor._thread.name == "photoculler-sysmon"
seen = []
for _ in range(6):
    time.sleep(0.45)
    seen.append(monitor.latest().ts)
monitor.stop()
assert monitor._thread is None
assert len(set(seen)) >= 2, "snapshot never advanced"
assert seen == sorted(seen), "timestamps must be monotonic"
# stop() is called from closeEvent and may run more than once.
monitor.stop()
# start() after stop() is allowed and must not stack threads.
monitor.start()
first = monitor._thread
monitor.start()
assert monitor._thread is first, "start() must not spawn a second thread"
monitor.stop()
print(f"[9] monitor thread: {len(set(seen))} distinct readings, stop/start safe")

# --- 10. the UI must not be the place sampling happens ------------------------
source = (Path(__file__).resolve().parent / "qt_ui.py").read_text(encoding="utf-8")
assert "sysmon.SystemMonitor()" in source, "the window no longer owns a monitor"
assert "system_monitor.start()" in source, "the monitor is never started"
assert "system_monitor.stop()" in source, "the monitor is never stopped"
assert "self._update_resource_panel()" in source
print("[10] the window owns, starts, polls and stops the monitor")

print("SYSMON TEST PASSED")
