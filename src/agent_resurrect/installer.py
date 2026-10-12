"""Apply and revert settings in order, and keep the install manifest that makes uninstall exact.

Manifest: {"version": 1, "installed_at": iso, "settings": {id: {"status": "changed" | "unchanged",
"original": dict | None}}}. A setting recorded as "changed" keeps its first original forever, so a reinstall
or upgrade never replaces the true original with an already-changed value.
"""
import json
import os
import shutil
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import paths
from .logs import log
from .model import utc_now_iso
from .settings import Setting, all_settings

MANIFEST_VERSION = 1
CHANGED = "changed"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
FAILED = "failed"
REVERTED = "reverted"


@dataclass
class StepResult:
    id: str
    status: str  # changed | unchanged | skipped | failed | reverted
    detail: str = ""


def _empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "installed_at": None, "settings": {}}


def _read_manifest() -> Optional[dict]:
    """The manifest on disk, an empty one if there is none, or None if it exists but cannot be read."""
    path = paths.manifest_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty_manifest()
    except (OSError, ValueError) as exc:
        log(f"manifest {path} is unreadable: {exc!r}")
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION \
            or not isinstance(data.get("settings"), dict):
        log(f"manifest {path} has an unknown format")
        return None
    return data


def load_manifest() -> dict:
    return _read_manifest() or _empty_manifest()


def _save_manifest(manifest: dict) -> None:
    path = paths.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _set_aside_unreadable_manifest() -> None:
    """Never silently overwrite a manifest we cannot read: it may hold the only copy of the originals."""
    path = paths.manifest_path()
    if path.exists() and _read_manifest() is None:
        aside = path.with_name(path.name + ".corrupt")
        os.replace(path, aside)
        log(f"moved unreadable manifest to {aside}")


def _failure(setting: Setting, action: str, exc: BaseException) -> StepResult:
    log(f"{action} {setting.id} failed: {exc!r}\n{traceback.format_exc()}")
    return StepResult(setting.id, FAILED, str(exc) or type(exc).__name__)


def install(settings: Optional[List[Setting]] = None) -> List[StepResult]:
    settings = all_settings() if settings is None else settings
    _set_aside_unreadable_manifest()
    manifest = load_manifest()
    manifest["installed_at"] = manifest.get("installed_at") or utc_now_iso()
    entries = manifest["settings"]
    results = []
    for setting in settings:
        try:
            original = setting.apply()
        except Exception as exc:
            results.append(_failure(setting, "install", exc))
            continue
        previous = entries.get(setting.id)
        first_change = previous is None or previous.get("status") != CHANGED
        if original is not None:
            if first_change:
                entries[setting.id] = {"status": CHANGED, "original": original}
            results.append(StepResult(setting.id, CHANGED, setting.description))
        else:
            try:
                check = setting.check()
            except Exception as exc:
                results.append(_failure(setting, "check", exc))
                continue
            if not check.ok:
                results.append(StepResult(setting.id, SKIPPED, check.detail))
                continue
            if previous is None:
                entries[setting.id] = {"status": UNCHANGED, "original": None}
            results.append(StepResult(setting.id, UNCHANGED, check.detail))
        _save_manifest(manifest)
    _save_manifest(manifest)
    return results


def _remove_tree(path: Path) -> bool:
    if path.name != paths.APP or not path.is_dir() or path.is_symlink():
        return False  # only ever remove our own directories
    shutil.rmtree(path)
    return True


def _remove_files(purge: bool, keep_manifest: bool) -> StepResult:
    removed = []
    if _remove_tree(paths.data_dir()):
        removed.append(str(paths.data_dir()))
    launcher = paths.bin_dir() / "agent-resurrect"
    alias = paths.bin_dir() / "resurrect"
    if alias.is_symlink() and os.readlink(alias) in ("agent-resurrect", str(launcher)):
        alias.unlink()
        removed.append(str(alias))
    if launcher.is_file() or launcher.is_symlink():
        launcher.unlink()
        removed.append(str(launcher))
    if purge and _remove_tree(paths.config_dir()):
        removed.append(str(paths.config_dir()))
    state = paths.state_dir()
    if purge and not keep_manifest:
        if _remove_tree(state):
            removed.append(str(state))
    elif purge and state.is_dir():
        for child in state.iterdir():
            if child.name == paths.manifest_path().name:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        removed.append(f"{state} (except the manifest)")
    elif not keep_manifest and paths.manifest_path().exists():
        paths.manifest_path().unlink()
        removed.append(str(paths.manifest_path()))
    return StepResult("files", REVERTED if removed else UNCHANGED,
                      ", ".join(removed) if removed else "nothing to remove")


def uninstall(settings: Optional[List[Setting]] = None, purge: bool = False) -> List[StepResult]:
    settings = all_settings() if settings is None else settings
    manifest = load_manifest()
    entries = manifest["settings"]
    results = []
    for setting in reversed(settings):
        entry = entries.get(setting.id)
        if entry is None:
            results.append(StepResult(setting.id, SKIPPED, "not changed by agent-resurrect"))
            continue
        if entry.get("status") == CHANGED:
            try:
                setting.revert(entry.get("original") or {})
            except Exception as exc:
                results.append(_failure(setting, "uninstall", exc))
                continue
            results.append(StepResult(setting.id, REVERTED, setting.description))
        else:
            results.append(StepResult(setting.id, UNCHANGED, "was already set before install; left as is"))
        del entries[setting.id]
        if paths.manifest_path().exists():
            _save_manifest(manifest)
    for unknown in entries:
        if all(setting.id != unknown for setting in settings):
            log(f"uninstall: no setting named {unknown} to revert; manifest entry was {entries[unknown]}")
    keep_manifest = any(r.status == FAILED for r in results)
    if keep_manifest:
        _save_manifest(manifest)
    results.append(_remove_files(purge, keep_manifest))
    return results
