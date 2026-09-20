from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def read_calibration_messages(path: Path) -> list[list[dict[str, str]]]:
    records: list[list[dict[str, str]]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            messages = record.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"{path}:{line_number}: messages must be a non-empty list")
            prompt_messages = messages[:-1] if messages[-1].get("role") == "assistant" else messages
            normalized = [
                {"role": str(message["role"]), "content": str(message["content"])}
                for message in prompt_messages
                if isinstance(message, dict) and "role" in message and "content" in message
            ]
            if not normalized:
                raise ValueError(f"{path}:{line_number}: no usable prompt messages")
            records.append(normalized)
    if not records:
        raise ValueError(f"no calibration records found in {path}")
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quantize the intent router to GPTQ for vLLM Marlin inference"
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--max-sequence-length", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scheme", choices=("W4A16", "W8A16"), default="W4A16")
    args = parser.parse_args()
    if args.samples < 1 or args.max_sequence_length < 128:
        parser.error("samples must be positive and max-sequence-length must be at least 128")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(f"output directory is not empty: {args.output}")
    return args


def main() -> int:
    args = parse_args()
    try:
        from datasets import Dataset
        from llmcompressor import oneshot
        try:
            from llmcompressor.modifiers.gptq import GPTQModifier
        except ImportError:
            from llmcompressor.modifiers.quantization.gptq import GPTQModifier
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "missing quantization dependencies; install transformers, datasets and llmcompressor"
        ) from exc

    messages = read_calibration_messages(args.dataset)
    rng = random.Random(args.seed)
    selected = rng.sample(messages, min(args.samples, len(messages)))

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    prompts = [
        tokenizer.apply_chat_template(
            item,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        for item in selected
    ]
    dataset = Dataset.from_dict({"text": prompts})

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
    recipe = GPTQModifier(targets="Linear", scheme=args.scheme, ignore=["lm_head"])
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
        "scheme": args.scheme,
        "algorithm": "GPTQ",
        "ignored_modules": ["lm_head"],
    }
    (args.output / "quantization_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
