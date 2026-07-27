#!/usr/bin/env python3
"""Build the ONNX INT8 quantized embedding model artifact (issue #353).

Exports the embedding model (DEFAULT_EMBEDDING_MODEL by default, or --model)
to ONNX and applies dynamic INT8 quantization, producing the on-disk
artifact that shiori.embedding._resolve_onnx_path() looks for at runtime.

This is a host-side, one-time (or occasional, e.g. on model upgrade) build
step -- it is NOT run in CI or in tests, and it is NOT run inside the app
image build (docker/app/Dockerfile intentionally does not bake the ONNX
artifact in; the artifact lives on the host and is bind-mounted read-only
into the app/ingest containers via ./models/onnx:/models/onnx:ro, see
docker-compose.yml). It requires network access to download the source
model from the Hugging Face Hub.

Run it via the app image, which already has the [onnx] extra installed
(most host venvs won't have optimum/onnxruntime). Three traps make the
naive `docker compose run app python scripts/...` fail, all verified on
2026-07-28: the image does not contain scripts/ (mount it), the compose
/models mount is read-only (write elsewhere), and the image bakes
HF_HUB_OFFLINE=1 (#238; re-enable network and point HF_HOME somewhere
writable):

    docker compose run --rm \
      -v "$PWD/scripts:/app/scripts:ro" \
      -v "$PWD/models:/models-out" \
      -e HF_HUB_OFFLINE=0 -e HF_HOME=/tmp/hf \
      app python scripts/build_onnx_model.py --output /models-out/onnx/e5-small-int8

Or, with a local venv that has `pip install '.[onnx]'`:

    python scripts/build_onnx_model.py --output models/onnx/e5-small-int8
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "models" / "onnx" / "e5-small-int8"


def _default_model() -> str:
    # Imported lazily so --help works without shiori's runtime deps installed.
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from shiori.config import DEFAULT_EMBEDDING_MODEL

    return DEFAULT_EMBEDDING_MODEL


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=None,
        help="Hugging Face model id to export (default: shiori's "
        "DEFAULT_EMBEDDING_MODEL).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory for the quantized model (default: {DEFAULT_OUTPUT}).",
    )
    return parser.parse_args(argv)


def build(model_name: str, output: Path) -> None:
    # Imported here, not at module scope: this script must still be
    # importable (and --help usable) in environments without the [onnx]
    # extra installed -- the extra is what actually provides these libs.
    from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoTokenizer

    output.mkdir(parents=True, exist_ok=True)

    print(f"Exporting {model_name!r} to ONNX...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.save_pretrained(output)

    ort_model = ORTModelForFeatureExtraction.from_pretrained(model_name, export=True)

    print("Quantizing (dynamic INT8, avx2/arm64 default config)...")
    quantizer = ORTQuantizer.from_pretrained(ort_model)
    qconfig = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
    quantizer.quantize(save_dir=output, quantization_config=qconfig)

    print(f"Done: {output}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    model_name = args.model or _default_model()
    build(model_name, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
