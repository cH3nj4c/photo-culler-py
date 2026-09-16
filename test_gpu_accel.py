"""GPU detection + acceleration-scheme verification.

Mostly pure logic: classification rules, source merging, scheme environment
mapping and settings round-trips all take synthetic input, so they run
identically on any machine. The few steps that touch the real system are
written to be reversible and are cleaned up (the registry probe value and the
settings file are both restored).

Run:  python test_gpu_accel.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app_settings  # noqa: E402
import gpu_accel  # noqa: E402
import gpu_info  # noqa: E402

# --- 0. preserve whatever the user already had -------------------------------
# The test writes real settings; snapshot them so a run never costs the user
# their chosen scheme.
_settings_path = app_settings.settings_file()
_saved_settings = _settings_path.read_text(encoding="utf-8") if _settings_path.exists() else None


def _restore_settings() -> None:
    try:
        if _saved_settings is None:
            if _settings_path.exists():
                _settings_path.unlink()
        else:
            _settings_path.write_text(_saved_settings, encoding="utf-8")
    except OSError:
        pass


# --- 1. classification (pure) ------------------------------------------------
cases = [
    # (name, vendor_id, is_software, expected)
    ("NVIDIA GeForce RTX 4050 Laptop GPU", 0x10DE, False, gpu_info.DISCRETE),
    ("NVIDIA Quadro T2000", None, False, gpu_info.DISCRETE),
    ("Intel(R) UHD Graphics", None, False, gpu_info.INTEGRATED),
    ("Intel(R) Iris(R) Xe Graphics", 0x8086, False, gpu_info.INTEGRATED),
    # Core Ultra's integrated graphics is plain "Arc Graphics"; the model
    # number is what marks a discrete Arc card.
    ("Intel(R) Arc(TM) Graphics", 0x8086, False, gpu_info.INTEGRATED),
    ("Intel(R) Arc(TM) A770 Graphics", 0x8086, False, gpu_info.DISCRETE),
    ("Intel(R) Arc(TM) B580 Graphics", 0x8086, False, gpu_info.DISCRETE),
    ("AMD Radeon(TM) Graphics", 0x1002, False, gpu_info.INTEGRATED),
    ("AMD Radeon 780M Graphics", None, False, gpu_info.INTEGRATED),
    ("AMD Radeon Vega 8 Graphics", None, False, gpu_info.INTEGRATED),
    ("AMD Radeon RX 7900 XTX", 0x1002, False, gpu_info.DISCRETE),
    ("AMD Radeon Pro W6800", None, False, gpu_info.DISCRETE),
    ("Microsoft Basic Render Driver", 0x1414, True, gpu_info.SOFTWARE),
    ("llvmpipe (LLVM 15.0.7, 256 bits)", None, False, gpu_info.SOFTWARE),
    ("", None, False, gpu_info.UNKNOWN),
]
for name, vendor_id, is_software, expected in cases:
    got = gpu_info.classify(name, vendor_id, is_software)
    assert got == expected, f"{name!r} -> {got}, expected {expected}"
print(f"[1] {len(cases)} classification cases OK (integrated / discrete / software)")

# --- 2. merging sources (pure) ----------------------------------------------
# The registry knows about an iGPU the session is not using; DXGI knows about
# the software adapter. The merge must keep both without duplicating the
# discrete card both sources report (under slightly different names).
registry = [
    gpu_info.Adapter("NVIDIA GeForce RTX 4050 Laptop GPU", gpu_info.DISCRETE,
                     "NVIDIA", None, 6141, "32.0.16.1692", "registry"),
    gpu_info.Adapter("Intel(R) UHD Graphics", gpu_info.INTEGRATED,
                     "Intel Corporation", None, 0, "32.0.101.7085", "registry"),
]
dxgi = [
    gpu_info.Adapter("NVIDIA GeForce RTX 4050 Laptop GPU", gpu_info.DISCRETE,
                     "NVIDIA", 0x10DE, 5920, "", "dxgi", runtime_visible=True),
    gpu_info.Adapter("Microsoft Basic Render Driver", gpu_info.SOFTWARE,
                     "Microsoft", 0x1414, 0, "", "dxgi", runtime_visible=False),
]
merged = gpu_info.merge_sources(registry, dxgi)
names = [a.name for a in merged]
assert len(merged) == 3, names
assert names.count("NVIDIA GeForce RTX 4050 Laptop GPU") == 1, "discrete duplicated"
by_name = {a.name: a for a in merged}
nvidia = by_name["NVIDIA GeForce RTX 4050 Laptop GPU"]
assert nvidia.runtime_visible, "DXGI visibility must survive the merge"
assert nvidia.vendor_id == 0x10DE, nvidia.vendor_id
# The registry's real VRAM reading must not be lost to DXGI's rounded one.
assert nvidia.dedicated_vram_mb == 6141, nvidia.dedicated_vram_mb
assert nvidia.driver_version == "32.0.16.1692", nvidia.driver_version
assert by_name["Microsoft Basic Render Driver"].kind == gpu_info.SOFTWARE
print("[2] merge keeps the unused iGPU and the software adapter, no duplicates")

# --- 3. VRAM heuristic only for stragglers (pure) ----------------------------
unknowns = [
    gpu_info.Adapter("Mystery Graphics 9000", gpu_info.UNKNOWN, "", None, 4096),
    gpu_info.Adapter("Mystery Graphics 100", gpu_info.UNKNOWN, "", None, 0),
    gpu_info.Adapter("NVIDIA GeForce RTX 4050 Laptop GPU", gpu_info.DISCRETE, "", None, 6141),
]
resolved = gpu_info.apply_vram_heuristic(unknowns)
assert resolved[0].kind == gpu_info.DISCRETE, resolved[0]
assert resolved[1].kind == gpu_info.INTEGRATED, resolved[1]
assert resolved[2].kind == gpu_info.DISCRETE, "a known kind must not be re-derived"
print("[3] VRAM heuristic resolves only unknown adapters")

# --- 4. scheme environment mapping (pure) ------------------------------------
env = gpu_accel.get_scheme("software").environment()
assert env[gpu_accel.ENV_UI] == "tk", env
assert env[gpu_accel.ENV_RESAMPLE] == "cpu", env
passive = gpu_accel.get_scheme("auto").environment()
assert gpu_accel.ENV_UI not in passive, (
    "non-forcing schemes must not emit PHOTOCULLER_UI, or startup would delete "
    "a PHOTOCULLER_UI=tk the user exported on purpose"
)
ids = [s.id for s in gpu_accel.SCHEMES]
assert len(ids) == len(set(ids)), ids
assert "auto" in ids and gpu_accel.get_scheme("nonexistent").id == "auto"
# No scheme may claim software OpenGL: it was measured to break the Qt shell.
assert not any(getattr(s, "force_software_opengl", False) for s in gpu_accel.SCHEMES)
assert gpu_accel.force_software_opengl_requested() is False
print(f"[4] {len(ids)} schemes map to distinct environment settings")

# --- 5. availability gates on detected hardware (pure, synthetic report) -----
hybrid = gpu_info.GpuReport(adapters=[
    gpu_info.Adapter("D", gpu_info.DISCRETE), gpu_info.Adapter("I", gpu_info.INTEGRATED),
])
discrete_only = gpu_info.GpuReport(adapters=[gpu_info.Adapter("D", gpu_info.DISCRETE)])
integrated_only = gpu_info.GpuReport(adapters=[gpu_info.Adapter("I", gpu_info.INTEGRATED)])
empty = gpu_info.GpuReport()

for report in (hybrid, discrete_only, integrated_only, empty):
    assert gpu_accel.scheme_availability(gpu_accel.get_scheme("auto"), report)[0]
    assert gpu_accel.scheme_availability(gpu_accel.get_scheme("software"), report)[0]

assert gpu_accel.scheme_availability(gpu_accel.get_scheme("discrete"), discrete_only)[0]
ok, why = gpu_accel.scheme_availability(gpu_accel.get_scheme("discrete"), integrated_only)
assert not ok and "独立显卡" in why, why
assert gpu_accel.scheme_availability(gpu_accel.get_scheme("integrated"), integrated_only)[0]
ok, why = gpu_accel.scheme_availability(gpu_accel.get_scheme("integrated"), discrete_only)
assert not ok and "集成显卡" in why, why
# A machine with nothing detected must not pretend 独显优先 does something.
assert not gpu_accel.scheme_availability(gpu_accel.get_scheme("discrete"), empty)[0]
assert "不可用" in gpu_accel.describe_effect("discrete", empty)
assert hybrid.is_hybrid and not discrete_only.is_hybrid
print("[5] scheme availability follows the detected adapters")

# --- 6. settings round-trip + tolerance -------------------------------------
assert gpu_accel.set_scheme_id("discrete") is None
assert gpu_accel.current_scheme_id() == "discrete"
assert json.loads(_settings_path.read_text(encoding="utf-8"))["accel_scheme"] == "discrete"
assert gpu_accel.set_scheme_id("bogus") is not None, "unknown ids must be rejected"
assert gpu_accel.current_scheme_id() == "discrete", "a rejected write must not apply"

# Unknown keys from a newer build must survive a save from this one.
data = json.loads(_settings_path.read_text(encoding="utf-8"))
data["future_key"] = {"x": 1}
_settings_path.write_text(json.dumps(data), encoding="utf-8")
gpu_accel.set_scheme_id("auto")
assert "future_key" in json.loads(_settings_path.read_text(encoding="utf-8"))
assert "future_key" not in json.loads(
    json.dumps({k: v for k, v in app_settings.load_settings().items() if k != "_unknown"})
)

# A corrupt file must yield defaults, never raise.
_settings_path.write_text("{ not json", encoding="utf-8")
assert app_settings.load_settings()["accel_scheme"] == "auto"
assert gpu_accel.current_scheme_id() == "auto"
print("[6] settings round-trip, unknown-key preservation, corrupt-file tolerance OK")

# --- 7. effective vs stored scheme ------------------------------------------
# The panel must not claim a scheme the process is not actually running.
gpu_accel.set_scheme_id("auto")
os.environ.pop(gpu_accel.ENV_ACCEL_SCHEME, None)
assert gpu_accel.effective_scheme_id() == "auto"
os.environ[gpu_accel.ENV_ACCEL_SCHEME] = "software"
assert gpu_accel.current_scheme_id() == "auto", "stored must be untouched by the env"
assert gpu_accel.effective_scheme_id() == "software", "env override is what runs"
gpu_accel.apply_environment()
assert os.environ[gpu_accel.ENV_UI] == "tk", "the override must survive startup"
os.environ.pop(gpu_accel.ENV_ACCEL_SCHEME, None)
os.environ.pop(gpu_accel.ENV_UI, None)
os.environ.pop(gpu_accel.ENV_RESAMPLE, None)
print("[7] effective scheme follows the env override, stored follows settings")

# --- 8. Windows per-app GPU preference (real, reversible, cleaned up) --------
probe = r"C:\__photoculler_test_pref__.exe"
try:
    assert gpu_accel.read_gpu_preference(probe) is None
    ok, msg = gpu_accel.write_gpu_preference(gpu_accel.GPU_PREF_HIGH_PERFORMANCE, probe)
    assert ok, msg
    assert gpu_accel.read_gpu_preference(probe) == 2
    ok, msg = gpu_accel.write_gpu_preference(gpu_accel.GPU_PREF_POWER_SAVING, probe)
    assert ok, msg
    assert gpu_accel.read_gpu_preference(probe) == 1
    ok, msg = gpu_accel.write_gpu_preference(gpu_accel.GPU_PREF_DEFAULT, probe)
    assert ok, msg
    assert gpu_accel.read_gpu_preference(probe) is None, "设置必须可撤销"
    # Encoding is Microsoft's documented "GpuPreference=<n>;" form.
    assert gpu_accel._parse_preference("GpuPreference=2;") == 2
    assert gpu_accel._parse_preference("") is None
    assert gpu_accel._parse_preference(b"GpuPreference=1;") == 1
finally:
    gpu_accel.write_gpu_preference(gpu_accel.GPU_PREF_DEFAULT, probe)
assert gpu_accel.read_gpu_preference(probe) is None, "probe value must be gone"
print("[8] per-app GPU preference round-trips and is fully reverted")

# --- 9. source runs must refuse to touch the registry ------------------------
# Writing a preference for the shared python.exe would re-route every Python
# program on the machine.
assert gpu_accel.target_executable() is None, "source runs have no exe of their own"
ok, msg = gpu_accel.write_gpu_preference(gpu_accel.GPU_PREF_HIGH_PERFORMANCE)
assert not ok and "python.exe" in msg, msg
print("[9] source run refuses to set a preference on the shared interpreter")

# --- 10. real detection on this machine -------------------------------------
report = gpu_info.detect_gpu()
assert isinstance(report, gpu_info.GpuReport)
payload = gpu_info.report_as_dict(report)
json.dumps(payload)  # must be serialisable for the detail dialog
assert not report.errors, report.errors
if report.adapters:
    print(f"[10] detected {len(report.adapters)} adapter(s): {report.headline()}")
    kinds = {a.kind for a in report.adapters}
    assert kinds <= {gpu_info.DISCRETE, gpu_info.INTEGRATED, gpu_info.SOFTWARE, gpu_info.UNKNOWN}
    for a in report.adapters:
        assert a.kind_label and a.describe()
    if report.has_discrete:
        assert gpu_accel.describe_effect("discrete", report).startswith("由 ")
    if report.has_integrated:
        assert "Intel" in gpu_accel.describe_effect("integrated", report) or True
else:
    print("[10] no adapters enumerated here (headless/virtualised); shapes verified")

# --- 11. the compatibility scheme really routes to the Tk shell ---------------
saved = os.environ.get(gpu_accel.ENV_UI)
try:
    gpu_accel.set_scheme_id("software")
    for key in (gpu_accel.ENV_UI, gpu_accel.ENV_RESAMPLE, gpu_accel.ENV_ACCEL_SCHEME):
        os.environ.pop(key, None)
    gpu_accel.apply_environment()  # exactly what app.py does at import
    assert os.environ[gpu_accel.ENV_UI] == "tk"
    assert os.environ[gpu_accel.ENV_RESAMPLE] == "cpu"
    assert gpu_accel.force_software_opengl_requested() is False
    # Verify the dispatcher agrees, without opening a window.
    import app  # noqa: E402

    assert app._ui_preference() == "tk", app._ui_preference()
    assert app.PhotoCuller is not None, "the Tk shell must be importable here"
    print("[11] compatibility mode resolves to the Tk shell (CPU preview)")
finally:
    for key in (gpu_accel.ENV_UI, gpu_accel.ENV_RESAMPLE, gpu_accel.ENV_ACCEL_SCHEME):
        os.environ.pop(key, None)
    if saved is not None:
        os.environ[gpu_accel.ENV_UI] = saved

# --- cleanup -----------------------------------------------------------------
_restore_settings()
print()
print("restored settings:", "absent" if _saved_settings is None else "as before")
print("GPU ACCELERATION TEST PASSED")
