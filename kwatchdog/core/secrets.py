"""Secrets: ``${VAR}`` / ``${VAR:-default}`` expansion from env + .env, and redaction.

Secrets never live in YAML: the YAML references env vars, the values are
expanded at load time and registered here so logs, stored raw output and
notifications can be scrubbed.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api_key|apikey|webhook|auth)", re.I)

_secret_values: set[str] = set()


class MissingEnvVar(KeyError):
    pass


def load_dotenv_files(*paths: Path) -> list[Path]:
    """Load .env files (without overriding real env). Works without python-dotenv."""
    loaded = []
    for p in paths:
        if not p or not p.is_file():
            continue
        try:
            from dotenv import dotenv_values  # optional

            values = dotenv_values(p)
        except ImportError:
            values = _parse_dotenv(p.read_text(encoding="utf-8"))
        for k, v in values.items():
            if v is not None and k not in os.environ:
                os.environ[k] = v
        loaded.append(p)
    return loaded


def _parse_dotenv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def expand(value: Any, *, missing: list[str] | None = None) -> Any:
    """Recursively expand ${VAR} in strings. Missing vars are collected (not raised)."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                val = os.environ[name]
            elif default is not None:
                val = default
            else:
                if missing is not None:
                    missing.append(name)
                return ""
            if val:
                register_secret_if_sensitive(name, val)
            return val

        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand(v, missing=missing) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, missing=missing) for v in value]
    return value


def register_secret_if_sensitive(name: str, value: str) -> None:
    # Every env-sourced value referenced by config is treated as potentially
    # sensitive, except obvious non-secrets that are too short to be useful.
    if len(value) >= 6:
        _secret_values.add(value)


def register_secret(value: str) -> None:
    if value and len(value) >= 4:
        _secret_values.add(value)


def redact(text: str) -> str:
    if not text or not _secret_values:
        return text
    for s in sorted(_secret_values, key=len, reverse=True):
        if s in text:
            text = text.replace(s, "***")
    return text


def literal_secret_warnings(raw: Any, path: str = "") -> list[str]:
    """Find secret-looking keys whose YAML value is a literal instead of ${VAR}."""
    out: list[str] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and SECRET_KEY_RE.search(str(k)) and v and not _VAR_RE.search(v):
                out.append(f"{p}: looks like a secret written literally in YAML; use ${{ENV_VAR}} instead")
            else:
                out.extend(literal_secret_warnings(v, p))
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            out.extend(literal_secret_warnings(v, f"{path}[{i}]"))
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def clear_secrets() -> None:
    _secret_values.clear()
