"""Capture a secret-free snapshot of the code, data, and local environment."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from app.core.config import settings


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "evaluation" / "v1"
OUTPUT = ROOT / "data" / "evaluation" / "stage7" / "reproduction_manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files() -> list[Path]:
    files = [ROOT / "main.py", ROOT / "requirements.txt", ROOT / "alembic.ini"]
    for directory in ("app", "scripts", "tests", "migrations"):
        files.extend((ROOT / directory).rglob("*.py"))
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    frozen = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    for filename, key in (("sources.json", "sources_sha256"), ("cases.json", "cases_sha256")):
        actual = sha256(DATASET / filename)
        if actual != frozen[key]:
            raise RuntimeError(f"v1 frozen dataset changed: {filename}")

    file_hashes = {path.relative_to(ROOT).as_posix(): sha256(path) for path in source_files()}
    fingerprint_input = "".join(f"{name} {digest}\n" for name, digest in file_hashes.items())
    requested = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line:
            package, pinned = line.split("==", 1)
            requested[package] = {"pinned": pinned, "installed": version(package)}

    commit = git_commit()
    report = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "code_version": {
            "git_commit": commit,
            "git_note": None if commit else "workspace has no Git commit available",
            "source_tree_sha256": hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest(),
            "source_file_count": len(file_hashes),
            "files": file_hashes,
        },
        "environment": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "packages": requested,
            "model_provider": settings.model_provider,
            "embedding_provider": settings.embedding_provider,
            "ollama_model": settings.ollama_model,
            "ollama_embedding_model": settings.ollama_embedding_model,
            "deepseek_key_configured": bool(settings.deepseek_api_key),
            "manual_operator_token_configured": bool(settings.manual_operator_token),
        },
        "evaluation_v1": {
            "role": frozen["role"],
            "source_count": frozen["source_count"],
            "chunk_count": frozen["chunk_count"],
            "case_count": frozen["case_count"],
            "sources_sha256": frozen["sources_sha256"],
            "cases_sha256": frozen["cases_sha256"],
            "previous_results_ollama_sha256": sha256(DATASET / "results_ollama.json"),
            "metrics_rerun_by_this_script": False,
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest": OUTPUT.relative_to(ROOT).as_posix(),
        "source_tree_sha256": report["code_version"]["source_tree_sha256"],
        "source_file_count": len(file_hashes),
        "frozen_dataset_verified": True,
        "git_commit": commit,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
