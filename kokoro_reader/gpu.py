"""NVIDIA runtime setup and GPU information.

The CUDA 12 / cuDNN 9 DLLs come from the nvidia-* pip packages inside the
project's virtual environment, so no system-wide CUDA installation is needed.
cuDNN loads some of its sub-libraries lazily through the normal DLL search
path, so the package folders must be on PATH as well as registered with
os.add_dll_directory, otherwise convolutions fail at the first inference.
"""

import glob
import os
import subprocess
from dataclasses import dataclass

_prepared = False


def prepare_cuda_dlls() -> list[str]:
    """Make the project-local NVIDIA DLLs loadable. Call before importing onnxruntime."""
    global _prepared
    dirs: list[str] = []
    try:
        import nvidia  # namespace package from the nvidia-*-cu12 wheels
    except ImportError:
        return dirs
    for base in list(nvidia.__path__):
        dirs.extend(sorted(glob.glob(os.path.join(base, "*", "bin"))))
    if not _prepared:
        for d in dirs:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
        _prepared = True
    return dirs


@dataclass
class GpuInfo:
    name: str = ""
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    driver: str = ""
    compute_cap: str = ""
    max_cuda: str = ""
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.name)

    def summary(self) -> str:
        if not self.found:
            return f"No NVIDIA GPU detected ({self.error})" if self.error else "No NVIDIA GPU detected"
        return (f"{self.name}, {self.vram_total_mb / 1024:.1f} GB VRAM, driver {self.driver}"
                f" (supports CUDA {self.max_cuda}), compute capability {self.compute_cap}")


def query_gpu() -> GpuInfo:
    """Read GPU model, VRAM and driver version from nvidia-smi (ships with the driver)."""
    info = GpuInfo()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
        if out.returncode != 0:
            info.error = out.stderr.strip() or "nvidia-smi failed"
            return info
        first = out.stdout.strip().splitlines()[0]
        name, total, free, driver, cap = [p.strip() for p in first.split(",")]
        info.name, info.driver, info.compute_cap = name, driver, cap
        info.vram_total_mb, info.vram_free_mb = int(float(total)), int(float(free))
        head = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10, creationflags=flags)
        for token in head.stdout.split("CUDA Version:")[1:2]:
            info.max_cuda = token.split()[0]
    except FileNotFoundError:
        info.error = "nvidia-smi not found"
    except Exception as e:  # noqa: BLE001 - informational only
        info.error = str(e)
    return info
