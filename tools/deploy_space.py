"""Deploy the committed tree (HEAD) to a Hugging Face Docker Space.

    python -m pip install huggingface_hub        # deploy-time only, not in requirements.txt
    python -m tools.deploy_space [owner/space-name]

Reads HF_TOKEN (a write token) and GEMINI_API_KEY from the environment or .env.
The Space gets GEMINI_API_KEY as a secret; it is never uploaded as a file.
Only committed files are deployed (git archive HEAD), so .env and local
databases cannot leak. The Space's README gets the front matter Spaces require;
the GitHub README stays clean.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FRONT_MATTER = """---
title: Invoice Decisioning
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: One invoice in, one explained AP decision out
---

"""


def _env(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    return None


def main() -> int:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("error: python -m pip install huggingface_hub", file=sys.stderr)
        return 1
    token = _env("HF_TOKEN")
    if not token:
        print("error: set HF_TOKEN (a write token from huggingface.co/settings/tokens) in .env", file=sys.stderr)
        return 1
    api = HfApi(token=token)
    space = sys.argv[1] if len(sys.argv) > 1 else _env("HF_SPACE") or f"{api.whoami()['name']}/invoice-decisioning"

    api.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True)
    gemini = _env("GEMINI_API_KEY")
    if gemini:
        api.add_space_secret(space, "GEMINI_API_KEY", gemini)
    else:
        api.add_space_variable(space, "LLM_CACHE_MODE", "offline")
        print("note: no GEMINI_API_KEY; the Space runs samples from recordings only")

    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, check=True,
                             capture_output=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        readme = Path(tmp) / "README.md"
        readme.write_text(FRONT_MATTER + readme.read_text(encoding="utf-8"), encoding="utf-8")
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
                                capture_output=True, text=True).stdout.strip()
        api.upload_folder(folder_path=tmp, repo_id=space, repo_type="space", delete_patterns="*",
                          commit_message=f"deploy {commit}")

    print(f"deployed: https://huggingface.co/spaces/{space}")
    print(f"app:      https://{space.replace('/', '-').replace('_', '-').lower()}.hf.space")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
