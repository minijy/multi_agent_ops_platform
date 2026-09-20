#!/usr/bin/env python3
"""Apply the narrow vLLM 0.28 Qwen warmup compatibility patch."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import py_compile
from pathlib import Path


VERSION = "0.28.0"
PATCH_MARKER = 'if any("MiniMaxM3" in str(architecture) for architecture in architectures):'
IMPORT_BLOCK = '''    from vllm.model_executor.warmup.minimax_m3_msa_warmup import (
        minimax_m3_msa_warmup,
    )

'''
CALL = "    minimax_m3_msa_warmup(worker)\n"
GUARDED_CALL = '''    # MiniMax M3 Triton kernels are unrelated to Qwen and can fail while
    # being imported eagerly. Load them only for an actual MiniMax M3 model.
    architectures = (
        getattr(worker.vllm_config.model_config.hf_config, "architectures", None)
        or []
    )
    if any("MiniMaxM3" in str(architecture) for architecture in architectures):
        from vllm.model_executor.warmup.minimax_m3_msa_warmup import (
            minimax_m3_msa_warmup,
        )

        minimax_m3_msa_warmup(worker)
'''


def main() -> None:
    installed = importlib.metadata.version("vllm")
    if installed != VERSION:
        raise SystemExit(
            f"refusing to patch vLLM {installed}; this patch is for {VERSION} only"
        )

    spec = importlib.util.find_spec("vllm")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("vLLM package directory was not found")
    target = (
        Path(next(iter(spec.submodule_search_locations)))
        / "model_executor/warmup/kernel_warmup.py"
    )
    source = target.read_text(encoding="utf-8")
    if PATCH_MARKER in source:
        print(f"vLLM warmup patch already applied: {target}")
        return
    if source.count(IMPORT_BLOCK) != 1 or source.count(CALL) != 1:
        raise SystemExit(f"unexpected vLLM warmup source; refusing to modify {target}")

    backup = target.with_suffix(".py.vllm-0.28.0.orig")
    if not backup.exists():
        backup.write_text(source, encoding="utf-8")
    patched = source.replace(IMPORT_BLOCK, "", 1).replace(CALL, GUARDED_CALL, 1)
    target.write_text(patched, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"applied vLLM warmup patch: {target}")


if __name__ == "__main__":
    main()
