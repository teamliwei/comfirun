"""Host memory purge routine, extracted from the SCAIL-2 headless pipeline.

Note: torch is imported lazily and only when already present in the server
environment; the server itself never requires it as a dependency.
"""
from __future__ import annotations

import ctypes
import gc
import platform


def purge_memory() -> None:
    """Aggressively reclaim system RAM (and GPU VRAM if torch is available)."""
    gc.collect()
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except ImportError:
        pass
    except Exception:
        pass
    if platform.system() == "Linux":
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
