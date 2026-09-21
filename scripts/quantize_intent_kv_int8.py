#!/usr/bin/env python3
"""Calibrate FP8 KV cache scales, optionally with W8A8 INT8 weights/activations."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def read_prompts(path: Path) -> list[list[dict[str, str]]]:
    prompts: list[list[dict[str, str]]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            messages = record.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"{path}:{line_number}: messages must be non-empty")
            if messages[-1].get("role") == "assistant":
                messages = messages[:-1]
            normalized = [
                {"role": str(message["role"]), "content": str(message["content"])}
                for message in messages
                if isinstance(message, dict)
                and "role" in message
                and "content" in message
            ]
            if normalized:
                prompts.append(normalized)
    if not prompts:
        raise ValueError(f"no calibration prompts found in {path}")
    return prompts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("fp8-kv", "w8a8-int8-fp8-kv"), required=True
    )
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--max-sequence-length", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--smoothquant-strength", type=float, default=0.8)
    args = parser.parse_args()
    if args.samples < 1 or args.max_sequence_length < 128:
        parser.error("invalid calibration size")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(f"output directory is not empty: {args.output}")
    return args


def main() -> int:
    args = parse_args()
    from compressed_tensors.quantization import QuantizationArgs
    from datasets import Dataset
    from llmcompressor import oneshot
    from llmcompressor.modifiers.quantization import QuantizationModifier
    from llmcompressor.modifiers.quantization.gptq import GPTQModifier
    from llmcompressor.modifiers.smoothquant import SmoothQuantModifier
    from transformers import AutoModelForCausalLM, AutoTokenizer

    all_prompts = read_prompts(args.dataset)
    selected = random.Random(args.seed).sample(
        all_prompts, min(args.samples, len(all_prompts))
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    rendered = [
        tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        for messages in selected
    ]
    dataset = Dataset.from_dict({"text": rendered})

    def tokenize(sample: dict[str, Any]) -> dict[str, Any]:
        return tokenizer(
            sample["text"],
            padding=False,
            truncation=True,
            max_length=args.max_sequence_length,
            add_special_tokens=False,
        )

    dataset = dataset.map(tokenize, remove_columns=dataset.column_names)
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        device_map="auto",
        trust_remote_code=True,
        torch_dtype="auto",
    )
    kv_cache = QuantizationArgs(
        num_bits=8,
        type="float",
        strategy="tensor",
        dynamic=False,
        symmetric=True,
    )
    if args.mode == "fp8-kv":
        recipe = QuantizationModifier(kv_cache_scheme=kv_cache)
    else:
        recipe = [
            SmoothQuantModifier(smoothing_strength=args.smoothquant_strength),
            GPTQModifier(
                targets="Linear",
                scheme="W8A8",
                ignore=["lm_head"],
                kv_cache_scheme=kv_cache,
            ),
        ]

    oneshot(
        model=model,
        dataset=dataset,
        recipe=recipe,
        max_seq_length=args.max_sequence_length,
        num_calibration_samples=len(selected),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.output, save_compressed=True)
    tokenizer.save_pretrained(args.output)
    manifest = {
        "source_model": str(args.model),
        "calibration_dataset": str(args.dataset),
        "calibration_samples": len(selected),
        "max_sequence_length": args.max_sequence_length,
        "seed": args.seed,
        "mode": args.mode,
        "kv_cache": "FP8 E4M3, static per-tensor calibrated scales",
        "weights_activations": (
            "BF16" if args.mode == "fp8-kv" else "W8A8 INT8 GPTQ + SmoothQuant"
        ),
        "smoothquant_strength": (
            args.smoothquant_strength
            if args.mode == "w8a8-int8-fp8-kv"
            else None
        ),
    }
    (args.output / "intent_quantization_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
