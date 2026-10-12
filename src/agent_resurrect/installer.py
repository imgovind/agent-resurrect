"""Apply and revert settings in order, and keep the install manifest that makes uninstall exact.

Manifest: {"version": 1, "installed_at": iso, "settings": {id: {"status": "changed" | "unchanged",
"original": dict | None}}}. A setting recorded as "changed" keeps its first original forever, so a reinstall
or upgrade never replaces the true original with an already-changed value.

A manifest that exists but cannot be used (unreadable, not JSON, an unknown format, or written by a newer
agent-resurrect) may hold the only copy of the user's original values, so install and uninstall then refuse:
one failed "manifest" step, and nothing is applied, reverted, deleted or moved.

Step statuses: changed, unchanged, skipped, failed, reverted. A dry run (dry_run=True) writes nothing at all,
not even the log, and reports would-change, would-revert, unchanged and skipped instead (failed when a check
itself fails or the manifest cannot be used).

A setting that is not applicable() is skipped without being applied; its optional `skip_detail` says why.
Settings an earlier version applied and this one does not (settings.legacy_settings()) are reverted and
dropped from the manifest by both install (the upgrade) and uninstall.
"""
import json
import os
import shutil
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import paths
from .logs import log
from .model import utc_now_iso
from .settings import Setting, all_settings, legacy_settings

MANIFEST_VERSION = 1
CHANGED = "changed"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
FAILED = "failed"
REVERTED = "reverted"
WOULD_CHANGE = "would-change"
WOULD_REVERT = "would-revert"
STATUSES = (CHANGED, UNCHANGED, SKIPPED, FAILED, REVERTED, WOULD_CHANGE, WOULD_REVERT)

MANIFEST_STEP = "manifest"  # the step id when the manifest cannot be used
FILES_STEP = "files"        # uninstall's last step: the launcher, the code and the manifest
LAUNCHER = "~/.local/bin/agent-resurrect"


@dataclass
class StepResult:
    id: str
    status: str            # one of STATUSES
    detail: str = ""       # one line: what happened, or why not
    description: str = ""  # what the step is about (the setting's description), for --json


class ManifestError(Exception):
    """The manifest exists but cannot be used. The message names the file and the reason."""

    def __init__(self, message: str, newer: bool = False):
        super().__init__(message)
        self.newer = newer  # written by a newer agent-resurrect: upgrading, not repairing, is the fix


def _empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "installed_at": None, "settings": {}}


def read_manifest() -> dict:
    """The manifest on disk, or an empty one if there is none. Writes nothing.
    Raises ManifestError when a file exists but cannot be used."""
    path = paths.manifest_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return _empty_manifest()
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{path} could not be read ({getattr(exc, 'strerror', None) or exc})") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"{path} is not valid JSON ({exc})") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > MANIFEST_VERSION:
        raise ManifestError(f"{path} was written by a newer agent-resurrect (manifest version {version}); "
                            "upgrade agent-resurrect first", newer=True)
    entries = data.get("settings") if isinstance(data, dict) else None
    if version != MANIFEST_VERSION or not isinstance(entries, dict) \
            or not all(isinstance(entry, dict) for entry in entries.values()):
        raise ManifestError(f"{path} has an unknown format")
    return data


def load_manifest() -> dict:
    """The manifest, or an empty one if there is none or it cannot be used."""
    try:
        return read_manifest()
    except ManifestError:
        return _empty_manifest()


def _save_manifest(manifest: dict) -> None:
    path = paths.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)  # the originals of the user's settings: private, like the records
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))  # mode 0600
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _manifest_or_refusal(verb: str, dry_run: bool):
    """(manifest, None), or (None, [failed step]) when the manifest exists but cannot be used."""
    try:
        return read_manifest(), None
    except ManifestError as exc:
        if not dry_run:
            log(f"refusing to {verb}: manifest {exc}")
        done = "changed" if verb == "install" else "reverted or deleted"
        detail = f"the install manifest {exc}; nothing was {done}"
        if not exc.newer:
            detail += ". It holds your original settings: restore or repair that file, then run this again"
        return None, [StepResult(MANIFEST_STEP, FAILED, detail, "Read the install manifest")]


def _failure(setting: Setting, action: str, exc: BaseException, dry_run: bool = False) -> StepResult:
    if not dry_run:
        log(f"{action} {setting.id} failed: {exc!r}\n{traceback.format_exc()}")
    return StepResult(setting.id, FAILED, str(exc) or type(exc).__name__, setting.description)


def _skip_detail(setting: Setting) -> str:
    return getattr(setting, "skip_detail", "") or "not applicable on this Mac"


def _migrate_legacy(manifest: dict, settings: List[Setting], dry_run: bool) -> List[StepResult]:
    """Install's part of an upgrade: revert ("changed") and drop the manifest entries of settings that an
    earlier version applied and this one no longer does."""
    entries = manifest["settings"]
    current = {setting.id for setting in settings}
    results = []
    for setting in legacy_settings():
        entry = entries.get(setting.id)
        if entry is None or setting.id in current:
            continue
        if entry.get("status") != CHANGED:  # it changed nothing, so there is nothing to give back
            if not dry_run:
                del entries[setting.id]
                _save_manifest(manifest)
                log(f"install: dropped the {setting.id} manifest entry; it had changed nothing")
            continue
        if dry_run:
            results.append(StepResult(setting.id, WOULD_REVERT, setting.description, setting.description))
            continue
        try:
            setting.revert(entry.get("original") or {})
        except Exception as exc:
            results.append(_failure(setting, "install: migrating", exc))  # the entry stays for the next install
            continue
        del entries[setting.id]
        _save_manifest(manifest)
        results.append(StepResult(setting.id, REVERTED, setting.description, setting.description))
    return results


def install(settings: Optional[List[Setting]] = None, dry_run: bool = False) -> List[StepResult]:
    settings = all_settings() if settings is None else settings
    manifest, refusal = _manifest_or_refusal("install", dry_run)
    if refusal:
        return refusal
    results = _migrate_legacy(manifest, settings, dry_run)
    if dry_run:
        return results + [_preview_install(setting) for setting in settings]
    manifest["installed_at"] = manifest.get("installed_at") or utc_now_iso()
    for setting in settings:
        results.append(_install_one(setting, manifest["settings"]))
        _save_manifest(manifest)
    _save_manifest(manifest)
    return results


def _preview_install(setting: Setting) -> StepResult:
    try:
        if not setting.applicable():
            return StepResult(setting.id, SKIPPED, _skip_detail(setting), setting.description)
        check = setting.check()
    except Exception as exc:
        return _failure(setting, "check", exc, dry_run=True)
    if check.ok:
        return StepResult(setting.id, UNCHANGED, check.detail, setting.description)
    return StepResult(setting.id, WOULD_CHANGE, setting.description, setting.description)


def _install_one(setting: Setting, entries: dict) -> StepResult:
    try:
        if not setting.applicable():
            return StepResult(setting.id, SKIPPED, _skip_detail(setting), setting.description)
        original = setting.apply()
    except Exception as exc:
        return _failure(setting, "install", exc)
    previous = entries.get(setting.id)
    if original is not None:
        if previous is None or previous.get("status") != CHANGED:
            entries[setting.id] = {"status": CHANGED, "original": original}
        return StepResult(setting.id, CHANGED, setting.description, setting.description)
    try:
        check = setting.check()
    except Exception as exc:
        return _failure(setting, "check", exc)
    if not check.ok:
        return StepResult(setting.id, SKIPPED, check.detail, setting.description)
    if previous is None:
        entries[setting.id] = {"status": UNCHANGED, "original": None}
    return StepResult(setting.id, UNCHANGED, check.detail, setting.description)


def _is_our_tree(path: Path) -> bool:
    return path.name == paths.APP and path.is_dir() and not path.is_symlink()  # only ever our own directories


def _removable(purge: bool) -> List[Path]:
    """The files uninstall removes, in order, that exist."""
    launcher = paths.bin_dir() / "agent-resurrect"
    alias = paths.bin_dir() / "resurrect"
    items = []
    if _is_our_tree(paths.data_dir()):
        items.append(paths.data_dir())
    if alias.is_symlink() and os.readlink(alias) in ("agent-resurrect", str(launcher)):
        items.append(alias)
    if launcher.is_file() or launcher.is_symlink():
        items.append(launcher)
    if purge and _is_our_tree(paths.config_dir()):
        items.append(paths.config_dir())
    if purge and _is_our_tree(paths.state_dir()):
        items.append(paths.state_dir())
    elif paths.manifest_path().exists():
        items.append(paths.manifest_path())
    # Last: this process runs with its bytecode cache in here, so nothing should be imported after it goes.
    if _is_our_tree(paths.cache_dir()):
        items.append(paths.cache_dir())
    return items


def _files_step(purge: bool, failed: bool, dry_run: bool) -> StepResult:
    description = "Remove agent-resurrect's program files and install manifest"
    if purge:
        description += ", and its records, log and configuration"
    if failed:
        # Without the launcher and the manifest entries the user could not retry, and a plugin left installed
        # would fail silently from then on.
        return StepResult(FILES_STEP, SKIPPED, "kept the launcher, the program files and the manifest so that "
                          f"uninstall can be retried; fix the failed step, then run: {LAUNCHER} uninstall",
                          description)
    items = _removable(purge)
    if not items:
        return StepResult(FILES_STEP, UNCHANGED, "nothing to remove", description)
    listed = ", ".join(str(item) for item in items)
    if dry_run:
        return StepResult(FILES_STEP, WOULD_REVERT, listed, description)
    for item in items:
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    return StepResult(FILES_STEP, REVERTED, listed, description)


def _uninstall_one(setting: Setting, manifest: dict, dry_run: bool) -> StepResult:
    entry = manifest["settings"][setting.id]
    if entry.get("status") == CHANGED:
        if dry_run:
            return StepResult(setting.id, WOULD_REVERT, setting.description, setting.description)
        try:
            setting.revert(entry.get("original") or {})
        except Exception as exc:
            return _failure(setting, "uninstall", exc)  # the entry stays, so a retry can revert it
        result = StepResult(setting.id, REVERTED, setting.description, setting.description)
    else:
        result = StepResult(setting.id, UNCHANGED, "was already set before install; left as is", setting.description)
    if not dry_run:
        del manifest["settings"][setting.id]
        if paths.manifest_path().exists():
            _save_manifest(manifest)
    return result


def uninstall(settings: Optional[List[Setting]] = None, purge: bool = False,
              dry_run: bool = False) -> List[StepResult]:
    settings = all_settings() if settings is None else settings
    manifest, refusal = _manifest_or_refusal("uninstall", dry_run)
    if refusal:
        return refusal
    entries = manifest["settings"]
    current = {setting.id for setting in settings}
    legacy = [setting for setting in legacy_settings() if setting.id not in current]
    results = []
    for setting in reversed(settings):
        if setting.id not in entries:
            results.append(StepResult(setting.id, SKIPPED, "not changed by agent-resurrect", setting.description))
        else:
            results.append(_uninstall_one(setting, manifest, dry_run))
    for setting in legacy:  # applied first by an earlier version, so reverted last
        if setting.id in entries:
            results.append(_uninstall_one(setting, manifest, dry_run))
    known = current | {setting.id for setting in legacy}
    for setting_id in [key for key in entries if key not in known]:
        if entries[setting_id].get("status") == CHANGED:
            # Written by a newer agent-resurrect that added a setting: never drop a change we cannot revert.
            results.append(StepResult(setting_id, FAILED, f"this version of agent-resurrect has no setting named "
                                      f"{setting_id} to revert; upgrade agent-resurrect, then uninstall"))
        elif not dry_run:
            log(f"uninstall: dropped the {setting_id} manifest entry; it had changed nothing")
            del entries[setting_id]
    failed = any(result.status == FAILED for result in results)
    if failed and not dry_run:
        log("uninstall: kept the files and manifest after a failed step: "
            + ", ".join(result.id for result in results if result.status == FAILED))
        _save_manifest(manifest)
    results.append(_files_step(purge, failed, dry_run))
    return results
