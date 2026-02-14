"""Create an OpenAI fine-tuning job from local JSONL files.

Supports both text and vision supervised fine-tuning.

You can either:
1) pass an existing --training-file JSONL, or
2) generate a vision JSONL from a manifest via --vision-manifest.

Vision manifest format (JSON):
[
  {
    "image": "./path/to/image1.jpg",
    "prompt": "What is shown in the image?",
    "answer": "A person raising both hands."
  }
]
"""

import argparse
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any


def _load_openai_client() -> Any:
    from openai import OpenAI

    return OpenAI()


def upload_file(client: Any, file_path: Path) -> str:
    with file_path.open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose="fine-tune")
    print(f"Uploaded {file_path} as {uploaded.id}")
    return uploaded.id


def _message_has_image_input(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "image_url" for item in content
    )


def validate_vision_training_file(training_file: Path) -> None:
    has_image_example = False

    with training_file.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {training_file} at line {line_number}: {exc}"
                ) from exc

            messages = row.get("messages")
            if isinstance(messages, list) and any(
                _message_has_image_input(m) for m in messages if isinstance(m, dict)
            ):
                has_image_example = True
                break

    if not has_image_example:
        raise ValueError(
            "Vision fine-tuning requires image inputs in the training dataset. "
            "Expected at least one content item with type='image_url'."
        )


def _encode_image_as_data_url(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(image_path))
    mime = mime or "image/jpeg"
    payload = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{payload}"


def build_vision_training_jsonl(
    vision_manifest: Path,
    output_training_file: Path,
    system_prompt: str,
) -> Path:
    rows = json.loads(vision_manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("Vision manifest must be a non-empty JSON list.")

    output_training_file.parent.mkdir(parents=True, exist_ok=True)

    with output_training_file.open("w", encoding="utf-8") as out:
        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"Manifest row #{idx} must be an object.")

            image_rel = row.get("image")
            prompt = row.get("prompt")
            answer = row.get("answer")
            if not image_rel or not prompt or not answer:
                raise ValueError(
                    f"Manifest row #{idx} requires 'image', 'prompt', and 'answer'."
                )

            image_path = (vision_manifest.parent / image_rel).resolve()
            if not image_path.exists():
                raise FileNotFoundError(
                    f"Manifest row #{idx} image not found: {image_path}"
                )

            example = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": _encode_image_as_data_url(image_path)
                                },
                            },
                        ],
                    },
                    {"role": "assistant", "content": answer},
                ]
            }
            out.write(json.dumps(example, ensure_ascii=False) + "\n")

    print(f"Built vision training JSONL: {output_training_file}")
    return output_training_file


def create_fine_tuning_job(
    training_file: Path,
    model: str,
    validation_file: Path | None = None,
    suffix: str | None = None,
    modality: str = "text",
    dry_run: bool = False,
) -> None:
    if modality == "vision":
        validate_vision_training_file(training_file)

    if dry_run:
        print("Dry run enabled: validated input and skipped API upload/job creation.")
        print(f"  Modality: {modality}")
        print(f"  Training file: {training_file}")
        print(f"  Validation file: {validation_file}")
        print(f"  Model: {model}")
        return

    client = _load_openai_client()
    training_file_id = upload_file(client, training_file)

    request: dict[str, Any] = {
        "training_file": training_file_id,
        "model": model,
    }

    if validation_file is not None:
        request["validation_file"] = upload_file(client, validation_file)

    if suffix:
        request["suffix"] = suffix

    job = client.fine_tuning.jobs.create(**request)

    print("Fine-tuning job created")
    print(f"  Modality: {modality}")
    print(f"  Job ID: {job.id}")
    print(f"  Status: {job.status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload training data and create an OpenAI fine-tuning job."
    )
    parser.add_argument(
        "--training-file",
        type=Path,
        default=None,
        help="Path to an existing JSONL training file.",
    )
    parser.add_argument(
        "--vision-manifest",
        type=Path,
        default=None,
        help="Optional manifest JSON used to build a vision training JSONL.",
    )
    parser.add_argument(
        "--output-training-file",
        type=Path,
        default=Path("examples/data/vision_fine_tuning_train.jsonl"),
        help="Output JSONL path when --vision-manifest is provided.",
    )
    parser.add_argument(
        "--vision-system-prompt",
        type=str,
        default="Use the image to answer the question.",
        help="System prompt used when building training rows from --vision-manifest.",
    )
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Base model to fine-tune (for example gpt-4o-2024-08-06 for vision).",
    )
    parser.add_argument(
        "--validation-file",
        type=Path,
        default=None,
        help="Optional JSONL validation file in chat fine-tuning format.",
    )
    parser.add_argument(
        "--suffix",
        type=str,
        default=None,
        help="Optional model suffix.",
    )
    parser.add_argument(
        "--modality",
        type=str,
        choices=["text", "vision"],
        default="text",
        help="Fine-tuning modality.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate/build data but skip API calls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    training_file = args.training_file

    if args.vision_manifest is not None:
        if not args.vision_manifest.exists():
            raise FileNotFoundError(f"Vision manifest not found: {args.vision_manifest}")
        training_file = build_vision_training_jsonl(
            vision_manifest=args.vision_manifest,
            output_training_file=args.output_training_file,
            system_prompt=args.vision_system_prompt,
        )

    if training_file is None:
        raise ValueError("Provide --training-file or --vision-manifest.")

    if not training_file.exists():
        raise FileNotFoundError(f"Training file not found: {training_file}")

    if args.validation_file is not None and not args.validation_file.exists():
        raise FileNotFoundError(f"Validation file not found: {args.validation_file}")

    create_fine_tuning_job(
        training_file=training_file,
        model=args.model,
        validation_file=args.validation_file,
        suffix=args.suffix,
        modality=args.modality,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
