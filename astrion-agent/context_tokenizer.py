"""Locally count model tokens and enforce explicit input budgets.

The count is exact for the supplied tokenizer and the documented JSON reference
serialization. Hosted APIs may apply a different hidden chat template; this is
not a guarantee about provider-side token billing or their context window.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from context_store import encode


class TokenCounterUnavailable(ValueError):
    pass


class LocalTokenCounter:
    def __init__(self, tokenizer, identifier):
        self.tokenizer = tokenizer
        self.identifier = identifier

    def count_text(self, text):
        if not isinstance(text, str):
            raise TypeError("token_count_requires_text")
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def count_request(self, messages, tools):
        # Stable reference representation, including roles, calls and schemas.
        # Not the provider's unpublished chat template.
        return self.count_text(encode({"messages": messages, "tools": tools or []}))


def load_token_counter(model=None):
    override = os.environ.get("ASTRION_CONTEXT_TOKENIZER_PATH", "").strip()
    if override:
        path = Path(override).expanduser()
    elif model in (None, "deepseek-flash", "deepseek-v4-flash"):
        path = (
            Path(__file__).resolve().parent
            / "research_data/tokenizers/deepseek-v4/tokenizer.json"
        )
    else:
        raise TokenCounterUnavailable("context_tokenizer_not_configured_for_model")
    if not path.is_file():
        raise TokenCounterUnavailable(
            "context_tokenizer_missing: set ASTRION_CONTEXT_TOKENIZER_PATH"
        )
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise TokenCounterUnavailable("context_tokenizers_package_missing") from exc
    data = path.read_bytes()
    if not override and hashlib.sha256(data).hexdigest() != DEEPSEEK_SHA256:
        raise TokenCounterUnavailable("context_default_tokenizer_sha256_mismatch")
    try:
        tokenizer = Tokenizer.from_file(str(path))
    except Exception as exc:
        raise TokenCounterUnavailable("context_tokenizer_invalid") from exc
    return LocalTokenCounter(
        tokenizer, "hf-tokenizer-sha256:" + hashlib.sha256(data).hexdigest()
    )


DEEPSEEK_URL = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"
DEEPSEEK_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"


def install_deepseek_tokenizer(destination=None):
    """Fetch only the official tokenizer file and verify a pinned hash."""
    import io
    import urllib.request
    import zipfile

    path = (
        Path(destination)
        if destination
        else Path(__file__).resolve().parent
        / "research_data/tokenizers/deepseek-v4/tokenizer.json"
    )
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() == DEEPSEEK_SHA256:
            return path
        raise ValueError("refuse_to_overwrite_existing_tokenizer")
    with urllib.request.urlopen(DEEPSEEK_URL, timeout=60) as response:
        blob = response.read(20_000_001)
    if len(blob) > 20_000_000:
        raise ValueError("tokenizer_download_too_large")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        info = archive.getinfo("deepseek_v4_tokenizer/tokenizer.json")
        if info.file_size > 10_000_000:
            raise ValueError("tokenizer_file_too_large")
        data = archive.read(info)
    if hashlib.sha256(data).hexdigest() != DEEPSEEK_SHA256:
        raise ValueError("tokenizer_sha256_mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents overwriting an existing file during setup.
    with path.open("xb") as file:
        file.write(data)
    return path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Install a pinned local tokenizer")
    parser.add_argument("command", choices=("install-deepseek",))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(install_deepseek_tokenizer(args.output))


if __name__ == "__main__":
    main()
