"""Explicitly provision the approved Section 13 model before runtime startup."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huggingface_hub import snapshot_download

from engram.constants import (
    APPROVED_SEMANTIC_ARTIFACT_SHA256,
    APPROVED_SEMANTIC_BACKEND,
    APPROVED_SEMANTIC_DIMENSION,
    APPROVED_SEMANTIC_LICENSE_ID,
    APPROVED_SEMANTIC_MODEL_ID,
    APPROVED_SEMANTIC_MODEL_VERSION,
)
from engram.semantic import model_artifact_sha256

DEFAULT_MODEL_ID = APPROVED_SEMANTIC_MODEL_ID
DEFAULT_REVISION = APPROVED_SEMANTIC_MODEL_VERSION
DEFAULT_LICENSE = APPROVED_SEMANTIC_LICENSE_ID
DEFAULT_DIMENSION = APPROVED_SEMANTIC_DIMENSION
NATIVE_ALLOW_PATTERNS = [
    "*.json",
    "*.txt",
    "*.model",
    "*.safetensors",
    "1_Pooling/*",
    "LICENSE",
    "README.md",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        default=f"data/artifacts/models/all-MiniLM-L6-v2-{DEFAULT_REVISION[:8]}",
        help="Local model directory; an existing checksum-matching artifact is reused.",
    )
    parser.add_argument("--revision", default=DEFAULT_REVISION, help="Immutable upstream commit SHA.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.revision != DEFAULT_REVISION:
        raise SystemExit(f"semantic model revision is not approved: {args.revision}")
    destination = Path(args.destination).resolve()
    manifest_path = destination.parent / f"{destination.name}.engram-model.json"
    expected_identity = {
        "schema_version": 1,
        "model_id": DEFAULT_MODEL_ID,
        "model_version": args.revision,
        "license_id": DEFAULT_LICENSE,
        "dimension": DEFAULT_DIMENSION,
        "backend": APPROVED_SEMANTIC_BACKEND,
        "runtime_downloads_allowed": False,
    }
    if destination.exists() and any(destination.iterdir()):
        if not manifest_path.is_file() or not (destination / "LICENSE").is_file():
            raise SystemExit(f"existing model destination is incomplete: {destination}")
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(existing_manifest, dict) or any(
            existing_manifest.get(name) != value for name, value in expected_identity.items()
        ):
            raise SystemExit(f"existing model manifest conflicts with the requested revision: {manifest_path}")
        checksum = model_artifact_sha256(destination)
        if existing_manifest.get("artifact_sha256") != checksum or checksum != APPROVED_SEMANTIC_ARTIFACT_SHA256:
            raise SystemExit(f"existing model artifact checksum mismatch: {destination}")
        manifest = {
            **expected_identity,
            "artifact_sha256": checksum,
            "model_path": str(destination),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({**manifest, "manifest_path": str(manifest_path), "reused": True}, sort_keys=True))
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=DEFAULT_MODEL_ID,
        revision=args.revision,
        local_dir=destination,
        allow_patterns=NATIVE_ALLOW_PATTERNS,
    )
    checksum = model_artifact_sha256(destination)
    if checksum != APPROVED_SEMANTIC_ARTIFACT_SHA256:
        raise SystemExit(f"downloaded model artifact checksum is not approved: {destination}")
    manifest = {
        **expected_identity,
        "artifact_sha256": checksum,
        "model_path": str(destination),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**manifest, "manifest_path": str(manifest_path), "reused": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
