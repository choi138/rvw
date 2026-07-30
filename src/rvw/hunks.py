"""Parse unified diff hunks and determine valid new-side anchors."""

from __future__ import annotations

import ast
import hashlib
import re

from pydantic import BaseModel, ConfigDict

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


class Hunk(BaseModel):
    """One unified-diff hunk and its new-side line classifications."""

    model_config = ConfigDict(extra="forbid")

    file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added_lines: set[int]
    context_lines: set[int]

    @property
    def hunk_id(self) -> str:
        return (
            f"{self.file}@@-{self.old_start},{self.old_count}+{self.new_start},{self.new_count}@@"
        )


def _header_path(raw: str) -> str | None:
    path = raw.split("\t", maxsplit=1)[0]
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        decoded = ast.literal_eval(path)
        if isinstance(decoded, str):
            path = decoded
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _hunk_from_header(header: re.Match[str], file: str) -> Hunk:
    return Hunk(
        file=file,
        old_start=int(header.group("old_start")),
        old_count=int(header.group("old_count") or "1"),
        new_start=int(header.group("new_start")),
        new_count=int(header.group("new_count") or "1"),
        added_lines=set(),
        context_lines=set(),
    )


def parse_hunks(diff: str) -> list[Hunk]:
    """Parse all hunks in a unified diff, preserving new-side line numbers."""

    hunks: list[Hunk] = []
    old_path: str | None = None
    file: str | None = None
    active: Hunk | None = None
    old_line = 0
    new_line = 0

    for line in diff.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            if file is None:
                continue
            active = _hunk_from_header(header, file)
            hunks.append(active)
            old_line = active.old_start
            new_line = active.new_start
            continue

        if active is not None:
            if line.startswith("+"):
                active.added_lines.add(new_line)
                new_line += 1
            elif line.startswith("-"):
                old_line += 1
            elif line.startswith(" "):
                active.context_lines.add(new_line)
                old_line += 1
                new_line += 1

            old_complete = old_line >= active.old_start + active.old_count
            new_complete = new_line >= active.new_start + active.new_count
            if old_complete and new_complete:
                active = None
            continue

        if line.startswith("--- "):
            old_path = _header_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _header_path(line[4:])
            file = new_path or old_path
            continue
        if line.startswith("diff --git "):
            old_path = None
            file = None

    return hunks


def hunk_sha256_by_id(diff: str) -> dict[str, str]:
    """Return SHA-256 digests of exact unified-diff hunk text by canonical hunk ID."""

    digests: dict[str, str] = {}
    old_path: str | None = None
    file: str | None = None
    active_id: str | None = None
    active_lines: list[str] = []

    def finish_active() -> None:
        nonlocal active_id, active_lines
        if active_id is not None:
            digests[active_id] = hashlib.sha256("".join(active_lines).encode()).hexdigest()
        active_id = None
        active_lines = []

    for raw_line in diff.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        header = _HUNK_HEADER.match(line)
        if header:
            finish_active()
            if file is not None:
                active_id = _hunk_from_header(header, file).hunk_id
                active_lines = [raw_line]
            continue
        if line.startswith("diff --git "):
            finish_active()
            old_path = None
            file = None
            continue
        if active_id is not None:
            active_lines.append(raw_line)
            continue
        if line.startswith("--- "):
            old_path = _header_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _header_path(line[4:])
            file = new_path or old_path

    finish_active()
    return digests


def hunk_for_line(hunks: list[Hunk], file: str, line: int) -> Hunk | None:
    """Return the hunk whose new-side range contains ``file:line``."""

    for hunk in hunks:
        if hunk.file == file and hunk.new_start <= line < hunk.new_start + hunk.new_count:
            return hunk
    return None


def is_anchorable(hunks: list[Hunk], file: str, line: int) -> bool:
    """Return whether ``file:line`` is an added line accepted by GitHub anchors."""

    hunk = hunk_for_line(hunks, file, line)
    return hunk is not None and line in hunk.added_lines
