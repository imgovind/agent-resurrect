"""Find the Claude Code sessions whose transcript contains some pasted text.

Ported from the tested resume-by-text script (find_session.py). Transcripts are JSONL files at
<CLAUDE_CONFIG_DIR>/projects/<dash-encoded cwd>/<session id>.jsonl. The search looks for short probes
taken from the paste and JSON-escaped, so text copied out of a terminal (wrapped lines, stripped markdown,
bullets, quotes) still matches the transcript it came from. Stdlib only.
"""
import json
import mmap
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import logs
from .model import Candidate, SessionInfo

# Runs of plain prose within one line. Markdown and box-drawing characters end
# a run, so probes survive terminal re-rendering (bullets, tables, indentation).
PLAIN_RUN = re.compile(r"[\w ,.'’:;()/-]+")
PROBE_WIDTH = 40  # measured: 60 straddles stripped markdown, 30 matches ~1,600 sessions by chance
MAX_PROBES = 10
MIN_TEXT_CHARS = 8
DESCRIBE_TOP = 8  # best raw matches that are read in full before the final ranking
# Bookkeeping records that merely echo a prompt already stored as a "user" record.
ECHO_TYPES = {"last-prompt", "queue-operation"}
FIRST_PROMPT_CHARS = 100
PERMISSION_TAIL_BYTES = 256 * 1024
PARALLEL_MIN_FILES = 256  # below this, starting worker processes costs more than it saves
SCAN_CHUNK = 32
# Session ids are UUIDs; anything else could be a path or a glob, so it is never looked up.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MODE_KEY = b'"permissionMode"'


@dataclass
class TranscriptMeta:
    cwd: Optional[str]          # the directory the session ran in, if the transcript records one
    cwd_trusted: bool           # True if cwd dash-encodes to the transcript's project folder name
    info: SessionInfo
    permission_mode: Optional[str]  # the last permission mode recorded anywhere in the transcript


def build_probes(text: str, max_probes: int = MAX_PROBES) -> List[str]:
    """Distinctive single-line substrings of the text that survive terminal wrapping.
    Raises ValueError if the text is too short to identify a session."""
    runs = {re.sub(r"\s+", " ", m.group()).strip() for m in PLAIN_RUN.finditer(text)}
    chosen: List[str] = []
    for min_len in (24, 12):
        # Longest first; ties broken alphabetically so the probe set does not depend on set order.
        chosen = sorted((r for r in runs if len(r) >= min_len), key=lambda r: (-len(r), r))
        if chosen:
            break
    probes = list(dict.fromkeys(_window(r) for r in chosen[:max_probes]))
    if not probes:
        whole = re.sub(r"\s+", " ", text).strip()
        if len(whole) < MIN_TEXT_CHARS:
            raise ValueError("Pasted text is too short to identify a session; paste a longer snippet.")
        probes = [whole]
    return probes


def _window(run: str) -> str:
    """Middle slice of a long run, trimmed to whole words."""
    if len(run) <= PROBE_WIDTH:
        return run
    start = (len(run) - PROBE_WIDTH) // 2
    chunk = run[start:start + PROBE_WIDTH]
    return chunk[chunk.find(" ") + 1:chunk.rfind(" ")] or chunk


def encode_probe(probe: str) -> bytes:
    """The probe as it appears inside a JSON string: quotes, backslashes and control characters escaped."""
    return json.dumps(probe, ensure_ascii=False)[1:-1].encode("utf-8")


def scan(job: Tuple[str, Sequence[bytes]]) -> Tuple[str, List[int]]:
    """Indexes of the probes found in one file. Module level so worker processes can unpickle it."""
    path, probes = job
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return path, [i for i, p in enumerate(probes) if mm.find(p) != -1]
    except (OSError, ValueError):  # ValueError: an empty file cannot be mmapped
        return path, []


def _executor(workers: int):
    from concurrent.futures import ProcessPoolExecutor  # imported here: the hook path never needs it
    return ProcessPoolExecutor(max_workers=workers)


def _scan_all(files: List[str], probes: List[bytes]) -> List[Tuple[str, List[int]]]:
    jobs = [(f, probes) for f in files]
    if len(jobs) >= PARALLEL_MIN_FILES:
        from concurrent.futures import BrokenExecutor
        workers = max(1, min(os.cpu_count() or 1, -(-len(jobs) // SCAN_CHUNK)))
        try:
            with _executor(workers) as pool:
                return list(pool.map(scan, jobs, chunksize=SCAN_CHUNK))
        except (OSError, NotImplementedError, BrokenExecutor) as exc:
            logs.log(f"finder: parallel scan unavailable ({exc}); scanning sequentially")
    return [scan(job) for job in jobs]


def _session_files(projects_dir: Path, exclude: Optional[str]) -> List[Path]:
    # Top level only: nested *.jsonl are subagent logs and research journals.
    try:
        projects = [p for p in projects_dir.iterdir() if p.is_dir()]
    except OSError:
        return []
    files: List[Path] = []
    for project in projects:
        try:
            files.extend(f for f in project.glob("*.jsonl") if f.stem != exclude)
        except OSError:
            continue
    return files


def _parse(raw: bytes) -> Optional[dict]:
    try:
        record = json.loads(raw)
    except ValueError:  # includes invalid UTF-8
        return None
    return record if isinstance(record, dict) else None


def _content(record: dict):
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def role_of(record: dict) -> str:
    """Who wrote a transcript record: "you", "Claude", "tool output", "injected context", or its type."""
    kind = record.get("type")
    if kind == "assistant":
        return "Claude"
    if kind == "user":
        content = _content(record)
        if record.get("isMeta"):
            return "injected context"
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content):
            return "tool output"
        return "you"
    return kind if isinstance(kind, str) and kind else "other"


def user_text(record: dict) -> str:
    content = _content(record)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def project_key(cwd: str) -> str:
    """The project folder name Claude Code derives from a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _mode_of(record: Optional[dict]) -> Optional[str]:
    value = record.get("permissionMode") if record else None
    return value if isinstance(value, str) and value else None


def _read_transcript(path: Path, probes: Sequence[bytes] = ()) -> Tuple[TranscriptMeta, Counter]:
    """One pass over a transcript: its metadata, and the roles of the records that contain any probe.
    Only lines that can matter are JSON-parsed. Raises OSError if the file cannot be read."""
    info = SessionInfo(last_active=path.stat().st_mtime)
    roles: Counter = Counter()
    cwds: List[str] = []
    mode = None
    need_meta = True
    with open(path, "rb") as fh:
        for raw in fh:
            hit = any(p in raw for p in probes)
            if not (hit or need_meta or b"ai-title" in raw or _MODE_KEY in raw):
                continue
            record = _parse(raw)
            if record is None:
                continue
            kind = record.get("type")
            title = record.get("aiTitle")
            if kind == "ai-title" and isinstance(title, str) and title.strip():
                info.title = title
            mode = _mode_of(record) or mode
            if need_meta:
                timestamp, cwd = record.get("timestamp"), record.get("cwd")
                if not info.started and isinstance(timestamp, str):
                    info.started = timestamp
                if isinstance(cwd, str) and cwd and cwd not in cwds:
                    cwds.append(cwd)
                if info.first_prompt is None and kind == "user" and role_of(record) == "you":
                    info.first_prompt = " ".join(user_text(record).split())[:FIRST_PROMPT_CHARS] or None
                need_meta = not (info.started and cwds and info.first_prompt)
            if hit and kind not in ECHO_TYPES:
                roles[role_of(record)] += 1
    # The folder name is the dash-encoded cwd the session started in, so only a
    # cwd that re-encodes to it is the directory `claude --resume` will search.
    folder = path.parent.name
    cwd = next((c for c in cwds if project_key(c) == folder), cwds[0] if cwds else None)
    trusted = cwd is not None and project_key(cwd) == folder
    return TranscriptMeta(cwd=cwd, cwd_trusted=trusted, info=info, permission_mode=mode), roles


def transcript_meta(path: Path) -> TranscriptMeta:
    """Title, opening prompt, timestamps, working directory and last permission mode of one transcript.
    Raises OSError if the file cannot be read."""
    return _read_transcript(Path(path))[0]


def last_permission_mode(path: Path) -> Optional[str]:
    """The last top-level "permissionMode" in the final 256 KB of a transcript; None if absent or unreadable.
    Lines are parsed, so a mode quoted inside a message or tool output (JSON-escaped) never counts."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - PERMISSION_TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    # A line cut in half by the seek cannot parse as a JSON object, so it is skipped like any bad line.
    for raw in reversed(tail.split(b"\n")):
        if _MODE_KEY in raw:
            mode = _mode_of(_parse(raw))
            if mode:
                return mode
    return None


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def find_transcript(session_id: str, projects_dir: Path) -> Optional[Path]:
    """<projects_dir>/<any project>/<session_id>.jsonl, the most recently written if there are several."""
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        return None
    try:
        projects = list(Path(projects_dir).iterdir())
    except OSError:
        return None
    best, best_mtime = None, -1.0
    for project in projects:
        path = project / f"{session_id}.jsonl"
        mtime = _mtime(path) if path.is_file() else None
        if mtime is not None and mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def _rank(candidate: Candidate) -> tuple:
    spoken = candidate.roles.get("you", 0) + candidate.roles.get("Claude", 0)
    # Coverage first, then whether the text appears in conversation (not only in
    # tool output, e.g. a session that merely grepped for it), then recency.
    return (-candidate.hits, -(spoken > 0), -(candidate.info.last_active or 0.0))


def search(text: str, projects_dir: Path, exclude: Optional[str] = None,
           limit: int = 5, max_probes: int = MAX_PROBES) -> List[Candidate]:
    """Sessions whose top-level transcript contains the text, best match first; [] if none.
    `exclude` is a session id to skip (normally the caller's own session, which contains the paste).
    Raises ValueError if the text is too short to search for."""
    probes = build_probes(text, max_probes)
    encoded = [encode_probe(p) for p in probes]
    files = [str(f) for f in _session_files(Path(projects_dir), exclude)]
    matches = []
    for path, hits in _scan_all(files, encoded):
        mtime = _mtime(Path(path)) if hits else None
        if mtime is not None:
            matches.append((Path(path), len(hits), mtime))
    matches.sort(key=lambda m: (-m[1], -m[2]))
    candidates = []
    for path, hits, _ in matches[:max(DESCRIBE_TOP, limit)]:
        try:
            meta, roles = _read_transcript(path, encoded)
        except OSError:  # deleted or unreadable since the scan
            continue
        candidates.append(Candidate(
            session_id=path.stem, transcript_path=str(path), cwd=meta.cwd, cwd_trusted=meta.cwd_trusted,
            hits=hits, total_probes=len(probes), roles=_ordered(roles), info=meta.info))
    candidates.sort(key=_rank)
    return candidates[:max(limit, 0)]


def _ordered(roles: Counter) -> Dict[str, int]:
    return dict(roles.most_common())
