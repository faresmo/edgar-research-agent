"""Settings, resolved once per run: flag > environment variable > config file > default.

See SPEC.md section 10.1. Every setting records where its value came from, so
`edgar-agent config` can show it and the run log can record it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:  # the same parser as a package, when installed
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None


def _load_toml(path) -> Dict[str, Any]:
    """The config file as a dict. Until 10 Nov the file was silently ignored on Python 3.9 and 3.10 (no tomllib there):
    settings were dropped without a word and save_setting rewrote the file with only the new setting. The file is flat
    (key = value, as save_setting writes it), so without a TOML parser it is read by the small reader below; a line that
    reader cannot understand raises, rather than being skipped."""
    if tomllib:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    out: Dict[str, Any] = {}
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") or "=" not in line:
            raise ValueError(f"{path}, line {n}: only 'key = value' lines are supported on this Python version "
                             "(install the 'tomli' package or use Python 3.11+ for full TOML)")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if val[:1] in ('"', "'"):
            quote = val[0]
            end = len(val) - 1
            while end > 0 and not (val[end] == quote and val[end - 1] != "\\"):
                end -= 1
            if end <= 0:
                raise ValueError(f"{path}, line {n}: unterminated string")
            out[key] = val[1:end].replace("\\" + quote, quote)
            continue
        val = val.split("#", 1)[0].strip()
        if val in ("true", "false"):
            out[key] = val == "true"
            continue
        try:
            out[key] = int(val)
        except ValueError:
            try:
                out[key] = float(val)
            except ValueError:
                raise ValueError(f"{path}, line {n}: cannot read the value {val!r}") from None
    return out

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "edgar-agent" / "config.toml"
ENV_PREFIX = "EDGAR_"

# Price per million tokens (input, output), from platform.claude.com on 6 Oct 2026.
# Used only for the cost estimate shown to the user; update when prices change.
MODEL_PRICES: Dict[str, tuple] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100k tokens; 5x above that
    "local": (0.0, 0.0),
}

DEFAULTS: Dict[str, Any] = {
    "source_mode": "strict",            # strict | extended  (SPEC §4 sourcing policy)
    "company_scope": "sec_filers",      # sec_filers | any  (8 Nov: answer only about companies that file with the SEC)
    "model": "claude-opus-5-5",         # orchestrator, or "local"
    "worker_model": "claude-haiku-4-5-20251001",  # extraction worker, or "same"
    "as_of": "",                        # YYYY-MM-DD; "" means today
    "web_search": "on",                 # on | off (discovery only either way)
    "max_cost_usd": 6.0,                # per question; a hard multi-document question legitimately costs $2-4 on Sonnet
    "max_turns_easy": 25,     # safety stops, not targets: a stuck model ends, a working one is not cut off
    "max_turns_medium": 40,
    "max_turns_hard": 60,
    "local_endpoint": "",               # OpenAI-compatible base URL for local mode
    "local_model": "",                  # model name on that server
    "answers_dir": "answers",
    "runs_dir": "runs",
    "tool_result_max_chars": 12000,
    "context_max_chars": 160000,
    "price_provider": "auto",           # extended mode only: Tiingo with the user's own token (EDGAR_PRICE_API_KEY); no token, no prices. Stooq was dropped on 22 Nov 2026     # about 40k tokens; older tool results are trimmed beyond this (§7.4)
    "max_output_tokens": 16000,     # per model reply; thinking models spend much of it before answering
}
VALID = {"source_mode": {"strict", "extended"}, "company_scope": {"sec_filers", "any"}, "web_search": {"on", "off"}, "price_provider": {"tiingo", "auto"}}


@dataclass
class Settings:
    values: Dict[str, Any] = field(default_factory=dict)
    origins: Dict[str, str] = field(default_factory=dict)  # setting -> flag | env | file | default

    def __getattr__(self, name: str) -> Any:
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def turn_budget(self, difficulty: str) -> int:
        """Turns are a safety stop, not a target. A local model takes smaller steps and
        its turns are cheap, so when the budget was not set explicitly it gets more room."""
        key = f"max_turns_{difficulty}"
        budget = int(self.values[key])
        if self.values.get("model") == "local" and self.origins.get(key) == "default":
            budget = {"easy": 40, "medium": 60, "hard": 80}.get(difficulty, budget)
        return budget

    def cost_cap(self) -> float:
        """The dollar cap scales with the model's price when it was left at its default: $6 is nine
        Sonnet questions and one and a half Fable questions (Fable hit it mid-answer on 31 Oct)."""
        cap = float(self.values["max_cost_usd"])
        if self.origins.get("max_cost_usd") == "default":
            price_in = MODEL_PRICES.get(str(self.values.get("model", "")), (2.0, 10.0))[0]
            cap *= max(1.0, price_in / 2.0)  # Sonnet's input price is the reference
        return cap

    def describe(self) -> str:
        width = max(len(k) for k in self.values)
        return "\n".join(f"{k.ljust(width)}  {self.values[k]!r:<32} ({self.origins[k]})" for k in self.values)

    def to_dict(self) -> dict:
        return {"values": dict(self.values), "origins": dict(self.origins)}


def _coerce(name: str, raw: Any) -> Any:
    default = DEFAULTS[name]
    if isinstance(default, bool):
        return str(raw).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return str(raw)


def resolve_settings(
    flags: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    config_path: Optional[Path] = DEFAULT_CONFIG_PATH,
) -> Settings:
    """Resolve every setting. `flags` are command-line values (None means not
    given); `env` defaults to os.environ; `config_path` None skips the file."""
    flags = flags or {}
    env = os.environ if env is None else env
    file_values: Dict[str, Any] = {}
    if config_path and Path(config_path).exists():
        file_values = _load_toml(config_path)

    settings = Settings()
    for name, default in DEFAULTS.items():
        if flags.get(name) is not None:
            value, origin = flags[name], "flag"
        elif env.get(ENV_PREFIX + name.upper()) not in (None, ""):
            value, origin = env[ENV_PREFIX + name.upper()], "env"
        elif name in file_values:
            value, origin = file_values[name], "file"
        else:
            value, origin = default, "default"
        value = _coerce(name, value)
        if name in VALID and value not in VALID[name]:
            raise ValueError(f"{name} must be one of {sorted(VALID[name])}, got {value!r} (from {origin})")
        settings.values[name] = value
        settings.origins[name] = origin
    if settings.values["model"] == "local" and not settings.values["local_endpoint"]:
        settings.origins["local_endpoint"] = "default (missing: local mode needs it)"
    return settings


def save_setting(name: str, value: Any, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Write one setting into the config file, keeping the others."""
    if name not in DEFAULTS:
        raise ValueError(f"unknown setting {name!r}")
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current: Dict[str, Any] = {}
    if path.exists():
        current = _load_toml(path)
    current[name] = value
    lines = []
    for key, val in current.items():
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"{key} = {val}")
        else:
            lines.append(f'{key} = "{str(val).replace(chr(34), chr(92) + chr(34))}"')
    path.write_text("\n".join(lines) + "\n")
