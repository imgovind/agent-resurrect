"""macOS user defaults: a thin wrapper over the `defaults` command, plus a Setting base class for boolean keys.

`defaults` is always run by name from PATH, so tests can put a fake first on PATH.
"""
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Check, Setting

GLOBAL_DOMAIN = "NSGlobalDomain"
_WRITE_FLAGS = {"boolean": "-bool", "integer": "-int", "float": "-float", "string": "-string"}
WRITABLE_TYPES = tuple(_WRITE_FLAGS)
_TIMEOUT = 30


class DefaultsError(RuntimeError):
    pass


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["defaults", *args], capture_output=True, text=True, timeout=_TIMEOUT,
                          stdin=subprocess.DEVNULL)


def _is_missing(proc: subprocess.CompletedProcess) -> bool:
    return "does not exist" in proc.stderr or "not found" in proc.stderr


def _fail(proc: subprocess.CompletedProcess, args: Sequence[str]) -> DefaultsError:
    message = (proc.stderr or proc.stdout).strip().splitlines()
    return DefaultsError(f"defaults {' '.join(args)} failed ({proc.returncode}): {message[-1] if message else ''}")


def read(domain: str, key: str) -> Optional[Tuple[str, str]]:
    """(type, value) as `defaults` reports them, e.g. ("boolean", "1"); None if the key is absent."""
    type_args = ["read-type", domain, key]
    typed = _run(type_args)
    if typed.returncode != 0:
        if _is_missing(typed):
            return None
        raise _fail(typed, type_args)
    type_ = typed.stdout.strip()
    if type_.startswith("Type is "):
        type_ = type_[len("Type is "):]
    value_args = ["read", domain, key]
    valued = _run(value_args)
    if valued.returncode != 0:
        if _is_missing(valued):
            return None
        raise _fail(valued, value_args)
    value = valued.stdout
    return type_, value[:-1] if value.endswith("\n") else value


def write(domain: str, key: str, type_: str, value: str) -> None:
    """type_ is boolean, integer, float or string; a boolean value may be given as "1"/"0" or "true"/"false"."""
    flag = _WRITE_FLAGS.get(type_)
    if flag is None:
        raise ValueError(f"cannot write a {type_} value with defaults")
    if type_ == "boolean":
        value = "TRUE" if as_bool("boolean", value) else "FALSE"  # `man defaults`: TRUE, FALSE, YES or NO
    args = ["write", domain, key, flag, value]
    proc = _run(args)
    if proc.returncode != 0:
        raise _fail(proc, args)


def delete(domain: str, key: str) -> None:
    """Delete the key; a key that is already absent is fine."""
    args = ["delete", domain, key]
    proc = _run(args)
    if proc.returncode != 0 and not _is_missing(proc):
        raise _fail(proc, args)


def as_bool(type_: str, value: str) -> bool:
    """The value -[NSUserDefaults boolForKey:] would return, which is how macOS and iTerm2 read these keys."""
    if type_ in ("boolean", "integer", "float"):
        try:
            return float(value) != 0
        except ValueError:
            return value.strip().lower() in ("true", "yes")
    if type_ == "string":  # -[NSString boolValue]
        text = value.lstrip()
        if text[:1] in ("+", "-"):
            text = text[1:]
        text = text.lstrip("0")
        return text[:1] in tuple("YyTt123456789")
    return False  # arrays, dictionaries, data and dates read as NO


@dataclass(frozen=True)
class BoolKey:
    domain: str
    key: str
    want: bool       # the desired value
    default: bool    # what the reading app assumes when the key is absent


class BoolDefaultsSetting(Setting):
    """A setting made of boolean defaults keys. The original stored for revert() is
    {"keys": {key: {"domain": d, "existed": bool, "type": t, "value": v}}}, covering only the keys apply() wrote."""

    keys: List[BoolKey] = []
    ok_detail: str = ""

    def _effective(self, spec: BoolKey, current: Optional[Tuple[str, str]]) -> bool:
        return spec.default if current is None else as_bool(*current)

    def _pending(self) -> List[Tuple[BoolKey, Optional[Tuple[str, str]]]]:
        pending = []
        for spec in self.keys:
            current = read(spec.domain, spec.key)
            if self._effective(spec, current) != spec.want:
                pending.append((spec, current))
        return pending

    def check(self) -> Check:
        pending = self._pending()
        if not pending:
            return Check(True, self.ok_detail)
        wrong = ", ".join(f"{spec.domain} {spec.key} is not {str(spec.want).lower()}" for spec, _ in pending)
        return Check(False, wrong)

    def apply(self) -> Optional[dict]:
        pending = self._pending()
        for spec, current in pending:
            if current is not None and current[0] not in WRITABLE_TYPES:
                raise RuntimeError(f"{spec.domain} {spec.key} holds a {current[0]} value that could not be "
                                   "restored on uninstall; change it by hand")
        changed: Dict[str, dict] = {}
        try:
            for spec, current in pending:
                write(spec.domain, spec.key, "boolean", "1" if spec.want else "0")
                changed[spec.key] = {"domain": spec.domain, "existed": current is not None,
                                     "type": current[0] if current else None,
                                     "value": current[1] if current else None}
        except Exception:
            self.revert({"keys": changed})
            raise
        return {"keys": changed} if changed else None

    def revert(self, original: dict) -> None:
        domains = {spec.key: spec.domain for spec in self.keys}
        for key, entry in (original or {}).get("keys", {}).items():
            domain = entry.get("domain") or domains[key]
            if entry.get("existed"):
                write(domain, key, entry["type"], entry["value"])
            else:
                delete(domain, key)
