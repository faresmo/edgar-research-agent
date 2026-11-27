"""Builds the clients the command line needs."""

from __future__ import annotations

import edgar_core as core

from .clients import AnthropicClient, ModelError, OpenAICompatibleClient
from .config import Settings


def make_edgar_client():
    return core.EdgarClient()


def make_web_search(settings: Settings):
    return None  # optional provider, not yet wired; search_filings covers EDGAR


def make_worker_client(settings: Settings, main_client):
    """The cheap reading model for extract(). Local mode reads with the same local model;
    Claude mode uses worker_model (Haiku by default), or the main model when 'same'."""
    if settings.model == "local" or str(settings.worker_model) in ("same", "", None):
        return main_client
    try:
        return AnthropicClient(str(settings.worker_model), max_tokens=4096)
    except ModelError:
        return main_client


def make_model_client(settings: Settings):
    if settings.model == "local":
        client = OpenAICompatibleClient(settings.local_endpoint, settings.local_model, max_tokens=int(settings.max_output_tokens))
        try:
            names = client.list_models()
        except ModelError as exc:
            raise SystemExit(str(exc))
        if names and settings.local_model not in names:
            raise SystemExit(f"model {settings.local_model!r} is not loaded on {settings.local_endpoint}. "
                             f"Available: {', '.join(names)}. Set EDGAR_LOCAL_MODEL or --local-model.")
        return client
    try:
        return AnthropicClient(settings.model, max_tokens=int(settings.max_output_tokens))
    except ModelError as exc:
        raise SystemExit(str(exc))
