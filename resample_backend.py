"""Preview crop/resize backends: DirectML → CUDA → CPU (always available).

UI stays on Tk. This module only turns a decoded image + crop box into a
display frame. GPU runtimes are optional; failures fall back to CPU.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from PIL import Image

# numpy is only needed by optional GPU backends (CuPy / DirectML).


@dataclass(frozen=True)
class ResampleRequest:
    image: Image.Image
    path_id: str
    source_box: tuple[int, int, int, int]
    target_size: tuple[int, int]
    interactive: bool


@dataclass(frozen=True)
class ResampleResult:
    image: Image.Image
    backend: str
    elapsed_ms: float


@dataclass(frozen=True)
class BackendInfo:
    kind: str  # "cpu" | "gpu"
    runtime: str  # "dml" | "cuda" | "cpu"
    device_name: str
    priority: int  # lower is preferred among available backends


@runtime_checkable
class ResampleBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def info(self) -> BackendInfo: ...

    def resample(self, req: ResampleRequest) -> ResampleResult: ...

    def shutdown(self) -> None: ...


def pil_resample_for(
    src_size: tuple[int, int], target_size: tuple[int, int], interactive: bool
) -> int:
    """LANCZOS only for mild shrink / enlarge; heavy downscale uses BILINEAR."""
    if interactive:
        return Image.Resampling.BILINEAR
    src_w, src_h = src_size
    if src_w <= 0 or src_h <= 0:
        return Image.Resampling.BILINEAR
    scale = min(target_size[0] / src_w, target_size[1] / src_h)
    if scale >= 0.5:
        return Image.Resampling.LANCZOS
    return Image.Resampling.BILINEAR


def _clamped_box(
    image: Image.Image, box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    x0 = max(0, min(x0, image.width))
    y0 = max(0, min(y0, image.height))
    x1 = max(x0 + 1, min(x1, image.width))
    y1 = max(y0 + 1, min(y1, image.height))
    return x0, y0, x1, y1


class CpuResampleBackend:
    name = "cpu"

    def available(self) -> bool:
        return True

    def info(self) -> BackendInfo:
        return BackendInfo("cpu", "cpu", "CPU", priority=100)

    def resample(self, req: ResampleRequest) -> ResampleResult:
        t0 = time.perf_counter()
        crop = req.image.crop(req.source_box)
        if crop.size != req.target_size:
            resample = pil_resample_for(crop.size, req.target_size, req.interactive)
            crop = crop.resize(req.target_size, resample)
        return ResampleResult(crop, self.name, (time.perf_counter() - t0) * 1000.0)

    def shutdown(self) -> None:
        return None


class CupyResampleBackend:
    """NVIDIA CUDA via CuPy (optional)."""

    name = "cuda"

    def __init__(self) -> None:
        self._cp = None
        self._probed = False
        self._device_name = "CUDA device"

    def available(self) -> bool:
        if self._probed:
            return self._cp is not None
        self._probed = True
        try:
            import cupy as cp  # type: ignore

            if cp.cuda.runtime.getDeviceCount() <= 0:
                return False
            a = cp.zeros((8, 8, 3), dtype=cp.uint8)
            _ = a[2:6, 2:6]
            self._cp = cp
            try:
                name = cp.cuda.runtime.getDeviceProperties(0)["name"]
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                self._device_name = str(name)
            except Exception:
                pass
        except Exception:
            self._cp = None
        return self._cp is not None

    def info(self) -> BackendInfo:
        return BackendInfo("gpu", "cuda", self._device_name, priority=20)

    def resample(self, req: ResampleRequest) -> ResampleResult:
        if self._cp is None:
            raise RuntimeError("CuPy not available")
        import numpy as np

        t0 = time.perf_counter()
        cp = self._cp
        arr = np.asarray(req.image)
        x0, y0, x1, y1 = _clamped_box(req.image, req.source_box)
        crop = arr[y0:y1, x0:x1]
        th, tw = req.target_size[1], req.target_size[0]
        ch, cw = crop.shape[0], crop.shape[1]
        gpu = cp.asarray(crop)
        if ch != th or cw != tw:
            ys = cp.linspace(0, ch - 1, th, dtype=cp.float32)
            xs = cp.linspace(0, cw - 1, tw, dtype=cp.float32)
            y0i = cp.clip(ys.astype(cp.int32), 0, ch - 1)
            x0i = cp.clip(xs.astype(cp.int32), 0, cw - 1)
            y1i = cp.clip(y0i + 1, 0, ch - 1)
            x1i = cp.clip(x0i + 1, 0, cw - 1)
            wy = (ys - y0i).reshape(-1, 1, 1)
            wx = (xs - x0i).reshape(1, -1, 1)
            g = gpu.astype(cp.float32)
            top = g[y0i][:, x0i] * (1 - wx) + g[y0i][:, x1i] * wx
            bot = g[y1i][:, x0i] * (1 - wx) + g[y1i][:, x1i] * wx
            out = top * (1 - wy) + bot * wy
            out = cp.clip(out, 0, 255).astype(cp.uint8)
        else:
            out = gpu
        host = cp.asnumpy(out)
        return ResampleResult(
            Image.fromarray(host, mode="RGB"),
            self.name,
            (time.perf_counter() - t0) * 1000.0,
        )

    def shutdown(self) -> None:
        self._cp = None


class DmlResampleBackend:
    """DirectML via torch-directml (optional; NVIDIA/AMD/Intel)."""

    name = "dml"

    def __init__(self) -> None:
        self._torch = None
        self._device = None
        self._probed = False
        self._device_name = "DirectML device"

    def available(self) -> bool:
        if self._probed:
            return self._device is not None
        self._probed = True
        try:
            import torch  # type: ignore
            import torch_directml  # type: ignore

            device = torch_directml.device()
            t = torch.zeros((1, 3, 8, 8), device=device)
            _ = float(t.mean().cpu())
            self._torch = torch
            self._device = device
            try:
                self._device_name = str(torch_directml.device_name(0))
            except Exception:
                pass
        except Exception:
            self._torch = None
            self._device = None
        return self._device is not None

    def info(self) -> BackendInfo:
        return BackendInfo("gpu", "dml", self._device_name, priority=10)

    def resample(self, req: ResampleRequest) -> ResampleResult:
        if self._torch is None or self._device is None:
            raise RuntimeError("DirectML not available")
        import numpy as np

        t0 = time.perf_counter()
        torch = self._torch
        arr = np.asarray(req.image)
        x0, y0, x1, y1 = _clamped_box(req.image, req.source_box)
        crop = np.ascontiguousarray(arr[y0:y1, x0:x1])
        th, tw = req.target_size[1], req.target_size[0]
        tensor = torch.from_numpy(crop).to(self._device)
        tensor = tensor.permute(2, 0, 1).unsqueeze(0).float()
        if tensor.shape[2] != th or tensor.shape[3] != tw:
            tensor = torch.nn.functional.interpolate(
                tensor, size=(th, tw), mode="bilinear", align_corners=False
            )
        tensor = tensor.squeeze(0).permute(1, 2, 0).clamp(0, 255).to(torch.uint8)
        host = tensor.cpu().numpy()
        return ResampleResult(
            Image.fromarray(host, mode="RGB"),
            self.name,
            (time.perf_counter() - t0) * 1000.0,
        )

    def shutdown(self) -> None:
        self._device = None
        self._torch = None


class ResampleService:
    """Pick DirectML → CUDA → CPU according to mode; sticky CPU after failures."""

    def __init__(self, mode: str | None = None) -> None:
        if mode is None:
            mode = os.environ.get("PHOTOCULLER_RESAMPLE") or "auto"
        self._mode = (mode or "auto").strip().lower()
        if self._mode in ("off", "software"):
            self._mode = "cpu"
        if self._mode in ("cupy", "cuda"):
            self._mode = "gpu"
        if self._mode not in ("auto", "cpu", "gpu"):
            self._mode = "auto"
        self._cpu = CpuResampleBackend()
        self._gpu_candidates: list[ResampleBackend] = [
            DmlResampleBackend(),
            CupyResampleBackend(),
        ]
        self._active: ResampleBackend | None = None
        self._active_name = ""
        self._fail_streak = 0
        self._locked_cpu = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_name(self) -> str:
        return self._active_name or "cpu"

    def start(self) -> str:
        if self._mode == "cpu":
            self._active = self._cpu
            self._active_name = self._cpu.name
            return self._active_name

        available: list[ResampleBackend] = []
        for backend in self._gpu_candidates:
            try:
                if backend.available():
                    available.append(backend)
            except Exception:
                continue
        if available:
            available.sort(key=lambda b: b.info().priority)
            self._active = available[0]
            self._active_name = self._active.name
        else:
            # auto/gpu with no accelerator → CPU
            self._active = self._cpu
            self._active_name = "cpu"
        return self._active_name

    def describe(self) -> str:
        if self._active is None:
            self.start()
        assert self._active is not None
        info = self._active.info()
        if info.kind == "cpu":
            return "重采样：CPU"
        return f"重采样：{info.runtime.upper()} ({info.device_name})"

    def resample(self, req: ResampleRequest) -> ResampleResult:
        if self._active is None:
            self.start()
        assert self._active is not None
        if self._locked_cpu or self._mode == "cpu" or self._active is self._cpu:
            return self._cpu.resample(req)
        try:
            result = self._active.resample(req)
            self._fail_streak = 0
            return result
        except Exception:
            self._fail_streak += 1
            if self._fail_streak >= 3:
                self._locked_cpu = True
                self._active = self._cpu
                self._active_name = "cpu"
            return self._cpu.resample(req)

    def shutdown(self) -> None:
        for backend in (*self._gpu_candidates, self._cpu):
            try:
                backend.shutdown()
            except Exception:
                pass
