"""Publishing dashboards: immutable numbered versions, rollback, and reading them back.

``easy-ui publish`` reads every dashboard in the project's ``ui/dashboards/``,
validates it against the CURRENT data bundle (the structure, then each query is
actually prepared in the sandbox and every column a component names is
checked) and only when everything is valid stores the files as a new,
read-only, numbered version and atomically switches ``current`` to it::

    <state>/specs/
        current -> versions/7          (relative symlink, switched atomically)
        versions/7/                    (folders 0755; files 0444, re-checked by hash on every read)
        versions/7/manifest.json       (number, time, note, data bundle, file hashes)
        versions/7/dashboards/<slug>.yaml
        .publish.lock                  (one publish / rollback at a time)

If anything fails nothing is switched and the error names the file and the
problem. The newest ``KEEP_VERSIONS`` (10) versions are kept; the current one
always stays. ``easy-ui rollback [n]`` switches ``current`` back instantly.

The server reads dashboards ONLY from ``specs/current`` through
``list_published`` / ``load_published`` / ``load_published_dashboard``; every
file is re-checked against the hash in the manifest before it is used.

File handling is hardened: symlinks, non-regular files, files over
``spec.LIMITS["file_bytes"]``, more than ``MAX_DASHBOARDS`` dashboards and
file names that are not valid slugs are refused. ``publish_from_archive`` takes
a tar (optionally gzip) stream for the later remote path and never calls
``tarfile`` extraction: only regular files named like dashboards are read,
with size, count and decompression limits, and written by easy-ui itself into
a fresh folder that is validated before the switch.

Public API
----------
Constants: ``KEEP_VERSIONS`` (10), ``MAX_DASHBOARDS`` (100), ``MAX_FILE_BYTES``,
``MAX_NOTE_LENGTH`` (200), ``MAX_ARCHIVE_BYTES`` (8 MB compressed),
``MAX_ARCHIVE_UNPACKED_BYTES`` (32 MB), ``MAX_ARCHIVE_MEMBERS``.

``SourceFile`` (dataclass): ``slug``, ``name``, ``label``, ``data`` (bytes), ``sha256``.
``read_dashboard_dir(directory, *, label_prefix="") -> (files, problems, notes)``
    Hardened listing + reading of a dashboards folder (used by publish and preview).
``validate_sources(files, bundle_path) -> list[dict]``
    ``[{"slug", "file", "title", "description", "ok", "errors", "warnings", "dashboard"}]``.

``read_archive(tar_stream) -> list[SourceFile]``  the hardened archive reader alone (raises ``Refused``).

``publish(config, *, note=None, keep=KEEP_VERSIONS, now=None) -> dict``
``publish_from_archive(where, tar_stream, *, note=None, keep=KEEP_VERSIONS, now=None) -> dict``
    ``where`` is an ``AppConfig`` or ``paths.StatePaths``; ``tar_stream`` is a
    binary file object or bytes. Hostile archives raise ``Refused`` (exit 5).
    Both return ``{"published", "unchanged", "version", "previous", "app",
    "published_at", "note", "bundle", "dashboards": [{slug, title, file,
    sha256, size}], "warnings", "pruned", "source"}`` and raise ``SpecError``
    (with ``problems``), ``NoData``, ``NotFound`` or ``TemporaryFailure``.
``list_versions(where) -> dict``   ``{"current", "versions": [...]}`` newest first.
``rollback(where, version=None, *, now=None) -> dict``
    ``None`` means the newest intact version older than the current one. Only the
    published version changes: the files in ui/dashboards/ stay as they are, so the
    command says when they differ (``files_match``) and how to copy the version back.
``restore_files(config, version=None, *, now=None) -> dict``
    Copies a version's files back into ui/dashboards/ (``rollback --restore-files``);
    files it would replace are moved to ``ui/dashboards/.before-restore-<time>/`` first.
``working_matches(config, version) -> bool``
``current_version(where) -> int | None``
``published_dir(where) -> Path | None``     the folder ``current`` points to.

Reading for the server (all take an ``AppConfig`` or ``paths.StatePaths``):
``PublishedSource(where)``  the same reads as an object with ``list()``,
    ``compiled(slug, today=None)``, ``dashboard(slug)``, ``bundle_path()`` and
    ``rev()``; ``easyui.preview.WorkingSource`` has the same methods, so the
    server can serve either.
``list_published(where) -> list[{"slug", "title", "description"}]``  (``[]`` when nothing is published)
``load_published_dashboard(where, slug) -> spec.Dashboard``   for ``spec.run_queries``.
``load_published(where, slug, *, today=None) -> dict``        the compiled spec (no SQL).
    Both raise ``NotFound`` for an unknown slug or when nothing is published,
    and ``PublishedDamaged`` (an ``EasyUIError``, HTTP 500, code
    ``published_damaged``) when a file no longer matches its hash.

``register(subparsers)``  adds ``publish``, ``versions``, ``rollback`` and (from
``easyui.preview`` / ``easyui.units``) ``preview`` and ``units`` to the CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from easyui import bundle, paths, spec, styles
from easyui.appconfig import AppConfig, write_snapshot
from easyui.errors import (
    EasyUIError,
    NotFound,
    Refused,
    SpecError,
    TemporaryFailure,
    UsageError,
)

KEEP_VERSIONS = 10
VERSION_DIR_MODE = 0o755
MAX_DASHBOARDS = 100
MAX_FILE_BYTES = spec.LIMITS["file_bytes"]
MAX_NOTE_LENGTH = 200
MAX_MANIFEST_BYTES = 1_000_000
MAX_ARCHIVE_BYTES = 8_000_000
MAX_ARCHIVE_UNPACKED_BYTES = 32_000_000
MAX_ARCHIVE_MEMBERS = MAX_DASHBOARDS + 8 + 1 + styles.MAX_STYLESHEETS + styles.MAX_ASSETS + 12
VERSION_RE = re.compile(r"^[1-9][0-9]{0,8}\Z")
_LINK_RE = re.compile(r"^versions/([1-9][0-9]{0,8})\Z")
_TMP_PREFIX = ".tmp-"
_ARCHIVE_DIRS = ("", "dashboards", "ui", "ui/dashboards")
_CACHE_MAX = 128
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class PublishedDamaged(EasyUIError):
    """A published file no longer matches the hash recorded when it was published."""

    http_status = 500
    default_code = "published_damaged"


def _state(where: AppConfig | paths.StatePaths) -> paths.StatePaths:
    return where.state if isinstance(where, AppConfig) else where


def _app_name(where: AppConfig | paths.StatePaths) -> str:
    return where.name if isinstance(where, AppConfig) else where.root.name


def _now(now: _dt.datetime | None) -> _dt.datetime:
    return (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)


def _iso(t: _dt.datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _problem(label: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return spec.Problem(label, "", message, None, hint).to_dict()


def _fsync_dir(d: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- reading a dashboards folder


@dataclass(frozen=True)
class SourceFile:
    slug: str
    name: str
    label: str
    data: bytes
    sha256: str


def _too_many(count: int) -> Refused:
    return Refused(
        f"There are {count} or more dashboard files, more than the {MAX_DASHBOARDS} easy-ui accepts, so nothing "
        "was published.",
        hint="Remove dashboards you no longer need (each file in ui/dashboards/ is one page), then try again.",
        code="too_many_dashboards",
    )


def _read_regular_at(dir_fd: int, name: str, label: str) -> bytes | dict[str, Any]:
    """Read one file below an open folder without following links; a problem dict when refused."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError as e:
        try:
            is_link = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            is_link = False
        if is_link:
            return _problem(label, f"{label} is a link to another file, which easy-ui does not follow.",
                            "Replace the link with the real file.")
        return _problem(label, f"{label} could not be opened ({e.strerror}).", "Check that the file is readable.")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _problem(label, f"{label} is not a plain file, so easy-ui does not read it.",
                            "Dashboards must be ordinary text files.")
        if st.st_size > MAX_FILE_BYTES:
            return _problem(label, f"{label} is {st.st_size} bytes, more than the {MAX_FILE_BYTES} bytes a dashboard "
                            "file may have.", "Split it into smaller dashboards or shorten the text.")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                return _problem(label, f"{label} is larger than the {MAX_FILE_BYTES} bytes a dashboard file may have.",
                                "Split it into smaller dashboards.")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_dashboard_dir(directory: Path, *, label_prefix: str = "") -> tuple[list[SourceFile], list[dict[str, Any]], list[str]]:
    """Every ``<slug>.yaml`` in ``directory``: (files, problems, notes). Never follows links."""
    files: list[SourceFile] = []
    problems: list[dict[str, Any]] = []
    notes: list[str] = []
    shown = label_prefix or (paths.display_path(directory) + "/")
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return files, problems, notes
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        problems.append(_problem(shown.rstrip("/"), f"{shown.rstrip('/')} is a link or not a folder, so easy-ui does not "
                                 "read dashboards from it.", "Make it a real folder holding the .yaml files."))
        return files, problems, notes
    dir_fd = os.open(directory, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    try:
        with os.scandir(dir_fd) as it:
            entries = sorted((e.name for e in it), key=str)
        ignored_yml, skipped_other = [], 0
        candidates = []
        for name in entries:
            if name.startswith("."):
                continue  # editor lock and swap files (".#x.yaml", ".x.yaml.swp")
            if name.endswith(".yml"):
                ignored_yml.append(name)
                continue
            if not name.endswith(".yaml"):
                skipped_other += 1
                continue
            candidates.append(name)
        if len(candidates) > MAX_DASHBOARDS:
            raise _too_many(len(candidates))
        for name in candidates:
            label = f"{shown}{name}"
            slug = name[: -len(".yaml")]
            if not spec.SLUG_RE.match(slug):
                problems.append(_problem(
                    label, f'The file name "{name}" cannot be used, because its name becomes the page address.',
                    "Name it <slug>.yaml with lowercase letters, digits and - (up to 41 characters, starting with a "
                    "letter or digit), for example training-overview.yaml."))
                continue
            got = _read_regular_at(dir_fd, name, label)
            if isinstance(got, dict):
                problems.append(got)
                continue
            files.append(SourceFile(slug, name, label, got, _sha256(got)))
        if ignored_yml:
            notes.append(f"Files ending in .yml are ignored ({', '.join(ignored_yml)}); rename them to .yaml.")
        if skipped_other:
            notes.append(f"{skipped_other} other file(s) or folder(s) in the dashboards folder were ignored.")
    finally:
        os.close(dir_fd)
    return files, problems, notes


def validate_sources(files: list[SourceFile], bundle_path: Path | None) -> list[dict[str, Any]]:
    """Structure and (with a bundle) data checks for each file; keeps the parsed Dashboard."""
    out = []
    for f in files:
        entry: dict[str, Any] = {"slug": f.slug, "file": f.label, "title": None, "description": None, "ok": False,
                                 "errors": [], "warnings": [], "dashboard": None}
        try:
            text = f.data.decode("utf-8")
        except UnicodeDecodeError:
            entry["errors"] = [_problem(f.label, f"{f.label} is not UTF-8 text.", "Save it as UTF-8 text.")]
            out.append(entry)
            continue
        dash, errors, warnings = spec.check_partial(text, slug=f.slug, file=f.label)
        complete = not errors
        if dash is not None and bundle_path is not None:
            try:
                e2, w2 = spec.check_with_data(dash, bundle_path, vary=complete)
            except EasyUIError as e:
                e2, w2 = [spec.Problem(f.label, "", e.message, None, e.hint)], []
            errors, warnings = errors + e2, warnings + w2
        if dash is not None:
            entry["title"], entry["description"] = dash.title, dash.description
        entry["errors"] = [p.to_dict() for p in errors]
        entry["warnings"] = [p.to_dict() for p in warnings]
        entry["ok"] = dash is not None and complete and not errors
        entry["dashboard"] = dash if entry["ok"] else None
        out.append(entry)
    return out


# ---------------------------------------------------------------- versions on disk


def _versions_dir(st: paths.StatePaths) -> Path:
    return st.specs / "versions"


def _current_link(st: paths.StatePaths) -> Path:
    return st.specs / "current"


def current_version(where: AppConfig | paths.StatePaths) -> int | None:
    st = _state(where)
    try:  # specs/ and versions/ are reached without following links (another account writes them)
        target = paths.readlink_nofollow(_current_link(st), 2)
    except OSError:
        return None
    m = _LINK_RE.match(target)
    if not m:
        return None
    d = _versions_dir(st) / m.group(1)
    try:
        s = paths.lstat_nofollow(d, 3)
    except OSError:
        return None
    return int(m.group(1)) if stat.S_ISDIR(s.st_mode) else None


def published_dir(where: AppConfig | paths.StatePaths) -> Path | None:
    v = current_version(where)
    return None if v is None else _versions_dir(_state(where)) / str(v)


def _version_numbers(st: paths.StatePaths) -> list[int]:
    vd = _versions_dir(st)
    out = []
    try:
        with os.scandir(vd) as it:
            for e in it:
                if VERSION_RE.match(e.name) and e.is_dir(follow_symlinks=False):
                    out.append(int(e.name))
    except FileNotFoundError:
        pass
    return sorted(out)


def _read_manifest(vdir: Path) -> dict[str, Any]:
    """The manifest of a version folder, checked; raises PublishedDamaged."""
    label = f"version {vdir.name}"
    try:
        fd = paths.open_nofollow(vdir / "manifest.json", 4)  # <ui>/specs/versions/<n>/manifest.json
    except OSError:
        raise PublishedDamaged(
            f"The published {label} has no readable description (manifest.json), so easy-ui cannot trust it.",
            hint="Publish again with: easy-ui publish (or roll back to another version).",
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PublishedDamaged(f"The description of {label} is not a plain file.", hint="Publish again.")
        raw = os.read(fd, MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(fd)
    try:
        m = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_MANIFEST_BYTES else None
    except (UnicodeDecodeError, ValueError):
        m = None
    ok = (
        isinstance(m, dict)
        and m.get("version") == int(vdir.name)
        and isinstance(m.get("dashboards"), list)
        and all(
            isinstance(d, dict)
            and isinstance(d.get("slug"), str)
            and spec.SLUG_RE.match(d["slug"])
            and isinstance(d.get("sha256"), str)
            and re.match(r"^[0-9a-f]{64}\Z", d["sha256"])
            for d in m["dashboards"]
        )
    )
    if ok and "styles" in m:
        st = m["styles"]
        ok = isinstance(st, list) and len(st) <= 1 + styles.MAX_STYLESHEETS + styles.MAX_ASSETS and all(
            isinstance(d, dict)
            and _style_path_ok(d.get("path"))
            and isinstance(d.get("sha256"), str)
            and re.match(r"^[0-9a-f]{64}\Z", d["sha256"])
            for d in st
        ) and len({d["path"] for d in st}) == len(st)
    if not ok:
        raise PublishedDamaged(
            f"The description of the published {label} is damaged, so easy-ui does not use it.",
            hint="Publish again with: easy-ui publish (or roll back to another version).",
        )
    return m  # type: ignore[return-value]


def _style_path_ok(p: Any) -> bool:
    """A path of a published style file: theme.yaml, styles/<name>.css or assets/<a>/<b>/<file>."""
    if not isinstance(p, str) or len(p) > 300:
        return False
    if p == "theme.yaml":
        return True
    parts = p.split("/")
    if parts[0] == "styles":
        return len(parts) == 2 and bool(styles.STYLESHEET_RE.match(parts[1]))
    if parts[0] == "assets":
        return 2 <= len(parts) <= 1 + styles.MAX_ASSET_DEPTH and all(styles.ASSET_SEGMENT_RE.match(x) for x in parts[1:])
    return False


def _style_limit(path: str) -> int:
    from easyui import css, theme  # noqa: PLC0415

    if path == "theme.yaml":
        return theme.MAX_THEME_BYTES
    return css.MAX_STYLESHEET_BYTES if path.startswith("styles/") else styles.MAX_ASSET_BYTES


def _read_version_style(vdir: Path, entry: dict[str, Any]) -> bytes:
    """One published style file, read without following links and checked against its hash."""
    rel = entry["path"]
    parts = rel.split("/")
    folder = vdir.joinpath(*parts[:-1])
    try:
        dfd = paths.open_nofollow(folder, 3 + len(parts) - 1, directory=True)
    except OSError:
        dfd = -1
    got: bytes | dict[str, Any] = {}
    if dfd >= 0:
        try:
            got = styles.read_at(dfd, parts[-1], rel, _style_limit(rel), "a published style file")
        finally:
            os.close(dfd)
    if isinstance(got, dict) or _sha256(got) != entry["sha256"]:
        raise PublishedDamaged(
            f"The published file {rel} (version {vdir.name}) was changed or damaged after it was published, so "
            "easy-ui does not use it.",
            hint="Publish again with: easy-ui publish, or roll back with: easy-ui rollback",
        )
    return got


def _version_style_files(vdir: Path, manifest: dict[str, Any]) -> styles.StyleFiles:
    files = styles.StyleFiles()
    for entry in manifest.get("styles") or []:
        data = _read_version_style(vdir, entry)
        b = styles.Blob(entry["path"], f"version {vdir.name}: {entry['path']}", data, entry["sha256"])
        if b.path == "theme.yaml":
            files.theme = b
        elif b.path.startswith("styles/"):
            files.stylesheets.append(b)
        else:
            files.assets.append(b)
    files.stylesheets.sort(key=lambda b: b.path)
    files.assets.sort(key=lambda b: b.path)
    return files


def _read_version_file(vdir: Path, entry: dict[str, Any]) -> bytes:
    slug = entry["slug"]
    try:
        dfd = paths.open_nofollow(vdir / "dashboards", 4, directory=True)
    except OSError:
        raise PublishedDamaged(
            f"The published version {vdir.name} has no readable dashboards folder, so easy-ui does not use it.",
            hint="Publish again with: easy-ui publish, or roll back with: easy-ui rollback",
        ) from None
    try:
        got = _read_regular_at(dfd, f"{slug}.yaml", f"{slug}.yaml")
    finally:
        os.close(dfd)
    if isinstance(got, dict) or _sha256(got) != entry["sha256"]:
        raise PublishedDamaged(
            f'The published dashboard "{slug}" (version {vdir.name}) was changed or damaged after it was published, '
            "so easy-ui does not show it.",
            hint="Publish again with: easy-ui publish, or roll back with: easy-ui rollback",
        )
    return got


def _verify_version(vdir: Path) -> dict[str, Any]:
    """Manifest + every file hash + structure; returns the manifest. Raises PublishedDamaged / SpecError."""
    m = _read_manifest(vdir)
    for entry in m["dashboards"]:
        data = _read_version_file(vdir, entry)
        spec.loads(data.decode("utf-8"), slug=entry["slug"], file=f"version {vdir.name}: {entry['slug']}.yaml")
    checked = styles.check(_version_style_files(vdir, m))
    if not checked.ok:
        first = checked.errors[0]
        raise PublishedDamaged(f"The look of version {vdir.name} does not pass easy-ui's checks: {first['problem']}",
                               hint="Publish again with: easy-ui publish, or pick another version.")
    return m


@contextlib.contextmanager
def _locked(st: paths.StatePaths) -> Iterator[None]:
    st.ensure()
    fh = open(st.specs / ".publish.lock", "a")  # noqa: SIM115 - held for the whole operation
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemporaryFailure(
                "Another publish or rollback for this project is running right now, so this one did not start.",
                hint="Wait a moment and try again.",
            ) from None
        yield
    finally:
        fh.close()



def _switch(st: paths.StatePaths, version: int) -> None:
    tmp = st.specs / f"{_TMP_PREFIX}{secrets.token_hex(6)}.link"
    os.symlink(f"versions/{version}", tmp)
    os.replace(tmp, _current_link(st))
    _fsync_dir(st.specs)


def _remove_tree(p: Path) -> None:
    """Remove a read-only version folder (never follows links)."""
    for root, dirs, _files in os.walk(p, followlinks=False):
        for d in dirs:
            dp = Path(root) / d
            if not dp.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(dp, 0o700)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)
    shutil.rmtree(p, ignore_errors=True)


def _prune(st: paths.StatePaths, keep: int) -> list[int]:
    keep = max(1, int(keep))
    cur = current_version(st)
    nums = _version_numbers(st)
    removed = []
    for n in nums[:-keep] if len(nums) > keep else []:
        if n == cur:
            continue
        _remove_tree(_versions_dir(st) / str(n))
        removed.append(n)
    cutoff = time.time() - 3600
    with contextlib.suppress(OSError):
        for e in os.scandir(st.specs):
            if e.name.startswith(_TMP_PREFIX):
                with contextlib.suppress(OSError):
                    if e.stat(follow_symlinks=False).st_mtime < cutoff:
                        if e.is_dir(follow_symlinks=False):
                            _remove_tree(Path(e.path))
                        else:
                            os.unlink(e.path)
    return removed


def _check_note(note: Any) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in note):
        raise UsageError(
            f"The note must be one line of text of at most {MAX_NOTE_LENGTH} characters.",
            hint='For example: --note "Added the sleep chart"',
        )
    return note.strip() or None


def _check_keep(keep: Any) -> int:
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= 1000:
        raise UsageError("--keep must be a number from 1 to 1000.", hint="For example: --keep 10")
    return keep


# ---------------------------------------------------------------- publishing


def _spec_error(results: list[dict[str, Any]], file_problems: list[dict[str, Any]]) -> SpecError:
    problems = list(file_problems)
    for r in results:
        problems.extend(r["errors"])
    first = problems[0]
    bad_files = list(dict.fromkeys(p["file"] for p in problems))
    more = f" (and {len(problems) - 1} more problem{'s' if len(problems) > 2 else ''})" if len(problems) > 1 else ""
    p = spec.Problem(first["file"], first.get("where", ""), first["problem"], first.get("line"), first.get("hint"),
                     first.get("allowed"), first.get("did_you_mean"), place=first.get("place"))
    return SpecError(
        f"Nothing was published, because {first['file']} has a mistake{more}: {p.text()}",
        hint=f"Fix {', '.join(bad_files[:5])}{' and others' if len(bad_files) > 5 else ''}, check with: easy-ui validate, "
        "then publish again. The dashboards people see now stay as they are.",
        problems=problems,
    )


def _publish_files(
    st: paths.StatePaths,
    app: str,
    files: list[SourceFile],
    file_problems: list[dict[str, Any]],
    *,
    note: str | None,
    keep: int,
    now: _dt.datetime | None,
    source: str,
    notes: list[str],
    style_files: styles.StyleFiles | None = None,
) -> dict[str, Any]:
    if len(files) > MAX_DASHBOARDS:
        raise _too_many(len(files))
    if not files and not file_problems:
        raise NotFound(
            "There are no dashboards to publish, so nothing was changed.",
            hint="Create one, e.g. ui/dashboards/overview.yaml (see: easy-ui components), check it with easy-ui "
            "validate, then publish.",
            code="no_dashboards",
        )
    current_bundle = bundle.require_current(st)
    results = validate_sources(files, current_bundle)
    style_files = style_files or styles.StyleFiles()
    style_check = styles.check(style_files)
    notes = [*notes, *style_check.notes]
    if file_problems or style_check.errors or not all(r["ok"] for r in results):
        raise _spec_error(results, [*file_problems, *style_check.errors])
    t = _now(now)
    warnings = [w for r in results for w in r["warnings"]] + style_check.warnings
    style_entries = [{"path": b.path, "sha256": b.sha256, "size": len(b.data)} for b in style_files.blobs()]
    style_key = sorted((d["path"], d["sha256"]) for d in style_entries)
    dashboards = [
        {"slug": f.slug, "title": r["title"], "description": r["description"], "file": f"dashboards/{f.slug}.yaml",
         "sha256": f.sha256, "size": len(f.data)}
        for f, r in zip(files, results, strict=True)
    ]
    with _locked(st):
        cur = current_version(st)
        if cur is not None:
            with contextlib.suppress(EasyUIError):
                old = _read_manifest(_versions_dir(st) / str(cur))
                if sorted((d["slug"], d["sha256"]) for d in old["dashboards"]) == sorted(
                    (d["slug"], d["sha256"]) for d in dashboards
                ) and sorted((d["path"], d["sha256"]) for d in old.get("styles") or []) == style_key:
                    return {
                        "published": False, "unchanged": True, "version": cur, "previous": cur, "app": app,
                        "published_at": old.get("published_at"), "note": old.get("note"), "bundle": current_bundle.stem,
                        "dashboards": old["dashboards"], "styles": old.get("styles") or [], "warnings": warnings,
                        "pruned": _prune(st, keep), "source": source, "notes": notes,
                    }
        nums = _version_numbers(st)
        version = (nums[-1] if nums else 0) + 1
        wanted = sorted((d["slug"], d["sha256"]) for d in dashboards)
        for n in reversed(nums):
            if cur is None or n <= cur:
                continue
            with contextlib.suppress(EasyUIError, OSError):
                other = _read_manifest(_versions_dir(st) / str(n))
                if sorted((d["slug"], d["sha256"]) for d in other["dashboards"]) == wanted and sorted(
                        (d["path"], d["sha256"]) for d in other.get("styles") or []) == style_key:
                    notes = [*notes, f"Note: these dashboards are the same as version {n}, which you rolled back "
                             f"from. If that was not meant, go back with: easy-ui rollback {cur}"]
                    break
        manifest = {
            "version": version,
            "app": app,
            "spec": spec.SPEC_VERSION,
            "published_at": _iso(t),
            "note": note,
            "source": source,
            "bundle": current_bundle.stem,
            "dashboards": dashboards,
            "styles": style_entries,
        }
        vd = _versions_dir(st)
        # 0750 on a server, where the web account reads the published versions through its group.
        st.specs.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.make_dir(vd, paths.dir_mode(st.root))
        staging = st.specs / f"{_TMP_PREFIX}{secrets.token_hex(6)}"
        try:
            staging.mkdir(mode=0o700)
            ddir = staging / "dashboards"
            ddir.mkdir(mode=0o700)
            for f in files:
                fd = os.open(ddir / f"{f.slug}.yaml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o444)
                try:
                    os.fchmod(fd, 0o444)  # not left to the umask: the web account (group) reads it on a server
                    os.write(fd, f.data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _write_styles(staging, style_files)
            mfd = os.open(staging / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o444)
            try:
                os.fchmod(mfd, 0o444)
                os.write(mfd, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                os.fsync(mfd)
            finally:
                os.close(mfd)
            # Files are read-only and checked against their hashes on every read. The folders stay writable
            # by their owner, so deleting the state folder to start over just works (rm -rf).
            os.chmod(ddir, VERSION_DIR_MODE)
            _verify_version_staged(staging, manifest)
            final = vd / str(version)
            os.rename(staging, final)
            os.chmod(final, VERSION_DIR_MODE)
        except OSError as e:
            _remove_tree(staging)
            raise TemporaryFailure(
                "The new version could not be saved, so nothing was published (the dashboards people see stay as "
                "they are).",
                hint="Check that there is free disk space and that the easy-ui state folder is writable, then try again.",
                detail=f"{type(e).__name__}: {e}",
            ) from None
        except BaseException:
            _remove_tree(staging)
            raise
        _fsync_dir(vd)
        _switch(st, version)
        pruned = _prune(st, keep)
    return {
        "published": True, "unchanged": False, "version": version, "previous": cur, "app": app,
        "published_at": manifest["published_at"], "note": note, "bundle": current_bundle.stem,
        "dashboards": dashboards, "styles": style_entries, "warnings": warnings, "pruned": pruned, "source": source,
        "notes": notes,
    }


def _write_styles(staging: Path, files: styles.StyleFiles) -> None:
    """The theme, stylesheets and assets of a new version: files 0444, folders 0755 (like the dashboards)."""
    made: list[Path] = []
    for b in files.blobs():
        target = staging.joinpath(*b.path.split("/"))
        for parent in reversed(target.relative_to(staging).parents[:-1]):
            d = staging / parent
            if not d.exists():
                d.mkdir(mode=0o700)
                made.append(d)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o444)
        try:
            os.fchmod(fd, 0o444)
            os.write(fd, b.data)
            os.fsync(fd)
        finally:
            os.close(fd)
    for d in made:
        os.chmod(d, VERSION_DIR_MODE)


def _verify_version_staged(staging: Path, manifest: dict[str, Any]) -> None:
    """Read back what was written before switching (the folder is not named like a version yet)."""
    for entry in manifest["dashboards"]:
        dfd = os.open(staging / "dashboards", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            got = _read_regular_at(dfd, f"{entry['slug']}.yaml", entry["slug"])
        finally:
            os.close(dfd)
        if isinstance(got, dict) or _sha256(got) != entry["sha256"]:
            raise TemporaryFailure(
                "The new version could not be written correctly, so nothing was published.",
                hint="Check there is free disk space, then publish again.",
            )
    for entry in manifest.get("styles") or []:
        parts = entry["path"].split("/")
        dfd = os.open(staging.joinpath(*parts[:-1]), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            got = styles.read_at(dfd, parts[-1], entry["path"], _style_limit(entry["path"]), "a style file")
        finally:
            os.close(dfd)
        if isinstance(got, dict) or _sha256(got) != entry["sha256"]:
            raise TemporaryFailure(
                "The new version could not be written correctly, so nothing was published.",
                hint="Check there is free disk space, then publish again.",
            )


def publish(config: AppConfig, *, note: str | None = None, keep: int = KEEP_VERSIONS, now: _dt.datetime | None = None) -> dict[str, Any]:
    note = _check_note(note)
    keep = _check_keep(keep)
    problems: list[dict[str, Any]] = []
    for p in (config.app_dir / "ui", config.dashboards_dir):
        if p.is_symlink():
            problems.append(_problem(paths.display_path(p, config.app_dir), f"{paths.display_path(p, config.app_dir)} is "
                                     "a link, so easy-ui does not read dashboards through it.",
                                     "Make it a real folder inside the project."))
    files: list[SourceFile] = []
    notes: list[str] = []
    style_files = styles.StyleFiles()
    if not problems:
        files, problems, notes = read_dashboard_dir(config.dashboards_dir, label_prefix="ui/dashboards/")
        style_files = styles.read_style_dir(config.app_dir / "ui")
    result = _publish_files(config.state, config.name, files, problems, note=note, keep=keep, now=now,
                            source="working folder", notes=notes, style_files=style_files)
    write_snapshot(config)
    return result


# ---------------------------------------------------------------- archives


def _refuse(message: str, hint: str = "Send a .tar or .tar.gz holding only the dashboard .yaml files.") -> Refused:
    return Refused(f"The archive was refused: {message} Nothing was published.", hint=hint, code="archive_refused")


def _read_limited(stream: BinaryIO | bytes | bytearray) -> bytes:
    if isinstance(stream, (bytes, bytearray)):
        raw = bytes(stream)
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise _refuse(f"it is larger than {MAX_ARCHIVE_BYTES} bytes.")
        return raw
    chunks, total = [], 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise UsageError("The archive must be read as bytes.", hint="Open the file in binary mode ('rb').")
        total += len(chunk)
        if total > MAX_ARCHIVE_BYTES:
            raise _refuse(f"it is larger than {MAX_ARCHIVE_BYTES} bytes.")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _gunzip_limited(raw: bytes) -> bytes:
    d = zlib.decompressobj(wbits=31)
    out, total = [], 0
    data = raw
    try:
        while data:
            chunk = d.decompress(data, 1 << 20)
            total += len(chunk)
            if total > MAX_ARCHIVE_UNPACKED_BYTES:
                raise _refuse(f"it unpacks to more than {MAX_ARCHIVE_UNPACKED_BYTES} bytes.")
            out.append(chunk)
            data = d.unconsumed_tail
        while not d.eof:
            chunk = d.decompress(b"", 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_UNPACKED_BYTES:
                raise _refuse(f"it unpacks to more than {MAX_ARCHIVE_UNPACKED_BYTES} bytes.")
            out.append(chunk)
    except zlib.error:
        raise _refuse("it is not a valid gzip file.") from None
    if not d.eof:
        raise _refuse("the gzip data is cut off.")
    if d.unused_data.strip(b"\0"):
        raise _refuse("there is extra data after the end of the gzip file.")
    return b"".join(out)


def _member_path(name: str) -> str | None:
    """The normalised relative path of an archive member, or None when it is unsafe."""
    if not name or len(name) > 255 or "\x00" in name or "\\" in name or name.startswith("/"):
        return None
    parts = name.split("/")
    while parts and parts[0] == ".":
        parts.pop(0)
    if parts and parts[-1] == "":
        parts.pop()
    if any(p in ("", ".", "..") for p in parts) or (parts and ":" in parts[0]):
        return None
    return "/".join(parts)


def read_archive(tar_stream: BinaryIO | bytes | bytearray) -> list[SourceFile]:
    """Hardened reading of a dashboards archive (the dashboards only); raises Refused for anything unexpected."""
    return read_archive_parts(tar_stream)[0]


def _style_member(rel: str) -> str | None:
    """The style path of an archive member (theme.yaml, styles/..., assets/..., also under ui/), else None."""
    r = rel[3:] if rel.startswith("ui/") else rel
    if r == "theme.yaml" or r.startswith(("styles/", "assets/")) or r in ("styles", "assets"):
        return r
    return None


def read_archive_parts(tar_stream: BinaryIO | bytes | bytearray) -> tuple[list[SourceFile], styles.StyleFiles]:
    """The dashboards and the style files of an archive; raises Refused for anything unexpected.

    Dashboards are ``<slug>.yaml`` at the top or in ``dashboards/`` (also under ``ui/``); the look is
    ``theme.yaml`` at the top, ``styles/<name>.css`` and ``assets/...`` (also under ``ui/``).
    """
    raw = _read_limited(tar_stream)
    if raw[:2] == b"\x1f\x8b":
        raw = _gunzip_limited(raw)
    elif len(raw) > MAX_ARCHIVE_UNPACKED_BYTES:
        raise _refuse(f"it is larger than {MAX_ARCHIVE_UNPACKED_BYTES} bytes.")
    if not raw:
        raise _refuse("it is empty.")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except (tarfile.TarError, EOFError, ValueError):
        raise _refuse("it is not a tar archive (only .tar and .tar.gz are accepted).") from None
    files: dict[str, SourceFile] = {}
    style_blobs: dict[str, styles.Blob] = {}
    count = 0
    with tf:
        while True:
            try:
                m = tf.next()
            except (tarfile.TarError, EOFError, ValueError, UnicodeError):
                raise _refuse("it is damaged or uses an unsupported format.") from None
            if m is None:
                break
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise _refuse(f"it has more than {MAX_ARCHIVE_MEMBERS} entries.",
                              f"Send at most {MAX_DASHBOARDS} dashboard files.")
            rel = _member_path(m.name)
            shown = repr(m.name[:80])
            if rel is None:
                raise _refuse(f"the entry {shown} has an unsafe name (an absolute path, '..', or odd characters).")
            srel = _style_member(rel)
            if m.isdir() and m.type == tarfile.DIRTYPE:
                if rel not in _ARCHIVE_DIRS and not (srel and (srel in ("styles", "assets") or _style_path_ok(srel))):
                    raise _refuse(f"it has a folder {shown}; only dashboard files are accepted.")
                continue
            if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or m.linkname or m.issparse():
                kind = ("a link" if (m.issym() or m.islnk()) else "a device or special file"
                        if (m.ischr() or m.isblk() or m.isfifo()) else "not a plain file")
                raise _refuse(f"the entry {shown} is {kind}; only plain dashboard files are accepted.")
            if srel is not None:
                if not _style_path_ok(srel):
                    raise _refuse(f"the entry {shown} is not a style file easy-ui accepts (theme.yaml, styles/<name>.css "
                                  "or a file in assets/ at most two folders deep).")
                if srel in style_blobs:
                    raise _refuse(f"the file {srel!r} appears more than once.", "Put each file in the archive once.")
                limit = _style_limit(srel)
                n_assets = sum(1 for k in style_blobs if k.startswith("assets/"))
                n_styles = sum(1 for k in style_blobs if k.startswith("styles/"))
                if m.size > limit:
                    raise _refuse(f"{srel} is {m.size} bytes, more than the {limit} such a file may have.")
                if srel.startswith("assets/") and n_assets >= styles.MAX_ASSETS or \
                        srel.startswith("styles/") and n_styles >= styles.MAX_STYLESHEETS:
                    raise _refuse(f"it has more style files than easy-ui accepts ({styles.MAX_STYLESHEETS} stylesheets, "
                                  f"{styles.MAX_ASSETS} assets).")
                try:
                    fh = tf.extractfile(m)
                    data = fh.read(limit + 1) if fh is not None else b""
                except (tarfile.TarError, EOFError, ValueError, OSError):
                    raise _refuse(f"{srel} is cut off or damaged.") from None
                if len(data) != m.size or len(data) > limit:
                    raise _refuse(f"{srel} is damaged (its size does not match).")
                style_blobs[srel] = styles.Blob(srel, f"archive:{srel}", data, _sha256(data))
                continue
            folder, _, name = rel.rpartition("/")
            if folder not in _ARCHIVE_DIRS or not name.endswith(".yaml"):
                raise _refuse(f"the entry {shown} is not a dashboard file (<slug>.yaml at the top or in dashboards/).")
            slug = name[: -len(".yaml")]
            if not spec.SLUG_RE.match(slug):
                raise _refuse(f"the file name {name!r} is not a valid dashboard name.",
                              "Use lowercase letters, digits and - (up to 41 characters), for example overview.yaml.")
            if slug in files:
                raise _refuse(f"the dashboard {slug!r} appears more than once.", "Put each dashboard in the archive once.")
            if m.size > MAX_FILE_BYTES:
                raise _refuse(f"{name} is {m.size} bytes, more than the {MAX_FILE_BYTES} a dashboard file may have.")
            if len(files) >= MAX_DASHBOARDS:
                raise _refuse(f"it has more than {MAX_DASHBOARDS} dashboards.")
            try:
                fh = tf.extractfile(m)
                data = fh.read(MAX_FILE_BYTES + 1) if fh is not None else b""
            except (tarfile.TarError, EOFError, ValueError, OSError):
                raise _refuse(f"{name} is cut off or damaged.") from None
            if len(data) != m.size or len(data) > MAX_FILE_BYTES:
                raise _refuse(f"{name} is damaged (its size does not match).")
            files[slug] = SourceFile(slug, name, f"archive:{name}", data, _sha256(data))
    if not files:
        raise _refuse("it has no dashboard files.", "Put the <slug>.yaml files at the top of the archive or in dashboards/.")
    style_files = styles.StyleFiles()
    for k in sorted(style_blobs):
        b = style_blobs[k]
        if k == "theme.yaml":
            style_files.theme = b
        elif k.startswith("styles/"):
            style_files.stylesheets.append(b)
        else:
            style_files.assets.append(b)
    return [files[k] for k in sorted(files)], style_files


def publish_from_archive(
    where: AppConfig | paths.StatePaths,
    tar_stream: BinaryIO | bytes | bytearray,
    *,
    note: str | None = None,
    keep: int = KEEP_VERSIONS,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    note = _check_note(note)
    keep = _check_keep(keep)
    files, style_files = read_archive_parts(tar_stream)
    return _publish_files(_state(where), _app_name(where), files, [], note=note, keep=keep, now=now,
                          source="archive", notes=[], style_files=style_files)


# ---------------------------------------------------------------- versions and rollback


def list_versions(where: AppConfig | paths.StatePaths) -> dict[str, Any]:
    st = _state(where)
    cur = current_version(st)
    items = []
    for n in reversed(_version_numbers(st)):
        vdir = _versions_dir(st) / str(n)
        item: dict[str, Any] = {"version": n, "current": n == cur, "ok": True}
        try:
            m = _read_manifest(vdir)
        except EasyUIError as e:
            item.update(ok=False, problem=e.message, published_at=None, note=None, dashboards=[])
        else:
            item.update(published_at=m.get("published_at"), note=m.get("note"), source=m.get("source"),
                        bundle=m.get("bundle"), dashboards=[d["slug"] for d in m["dashboards"]])
        items.append(item)
    return {"app": _app_name(where), "current": cur, "versions": items}


def rollback(where: AppConfig | paths.StatePaths, version: int | str | None = None, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    st = _state(where)
    if version is not None:
        if isinstance(version, bool) or not VERSION_RE.match(str(version).strip()):
            raise UsageError(f"{str(version)[:20]!r} is not a version number.",
                             hint="See the versions with: easy-ui versions")
        version = int(str(version).strip())
    with _locked(st):
        cur = current_version(st)
        nums = _version_numbers(st)
        if not nums:
            raise NotFound("Nothing has been published yet, so there is no version to go back to.",
                           hint="Publish first with: easy-ui publish")
        skipped: list[str] = []
        if version is None:
            if cur is None:
                raise NotFound("No version is in use right now, so there is no previous one.",
                               hint="Pick a version with: easy-ui rollback <number> (see: easy-ui versions)")
            target = None
            for n in reversed([n for n in nums if n < cur]):
                try:
                    _verify_version(_versions_dir(st) / str(n))
                except EasyUIError as e:
                    skipped.append(f"version {n}: {e.message}")
                    continue
                target = n
                break
            if target is None:
                raise NotFound(f"There is no usable version older than version {cur}, so nothing was changed.",
                               hint="See the versions with: easy-ui versions", code="no_previous_version")
        else:
            if version not in nums:
                raise NotFound(f"There is no published version {version}.",
                               hint=f"Versions kept: {', '.join(map(str, reversed(nums)))}. See: easy-ui versions")
            target = version
            if target != cur:
                try:
                    _verify_version(_versions_dir(st) / str(target))
                except EasyUIError as e:
                    raise Refused(
                        f"Version {target} cannot be used: {e.message} Nothing was changed.",
                        hint="Pick another version (see: easy-ui versions) or publish again.",
                        code="version_damaged",
                    ) from None
        if target == cur:
            return {"app": _app_name(where), "version": cur, "previous": cur, "changed": False, "skipped": skipped}
        _switch(st, target)
    return {"app": _app_name(where), "version": target, "previous": cur, "changed": True, "skipped": skipped,
            "switched_at": _iso(_now(now))}


def working_matches(config: AppConfig, version: int) -> bool:
    """True when ui/dashboards/ holds exactly the dashboards of ``version`` (so publish would not change it)."""
    st = config.state
    try:
        m = _read_manifest(_versions_dir(st) / str(version))
        files, problems, _notes = read_dashboard_dir(config.dashboards_dir, label_prefix="ui/dashboards/")
    except (EasyUIError, OSError):
        return False
    if problems:
        return False
    sf = styles.read_style_dir(config.app_dir / "ui")
    if sf.problems or sorted((b.path, b.sha256) for b in sf.blobs()) != sorted(
            (d["path"], d["sha256"]) for d in m.get("styles") or []):
        return False
    return sorted((f.slug, f.sha256) for f in files) == sorted((d["slug"], d["sha256"]) for d in m["dashboards"])


def restore_files(config: AppConfig, version: int | str | None = None, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Copy a published version's dashboard files back into ui/dashboards/ (default: the current version).

    Files there that would be replaced or that are not part of the version are first moved into
    ``ui/dashboards/.before-restore-<time>/`` (ignored by easy-ui because the name starts with a dot), so
    nothing is lost. Never follows links.
    """
    st = config.state
    if version is None:
        version = current_version(st)
        if version is None:
            raise NotFound("Nothing has been published yet, so there is no version to copy back.",
                           hint="Publish first with: easy-ui publish")
    elif isinstance(version, bool) or not VERSION_RE.match(str(version).strip()):
        raise UsageError(f"{str(version)[:20]!r} is not a version number.", hint="See the versions with: easy-ui versions")
    version = int(str(version).strip())
    vdir = _versions_dir(st) / str(version)
    if not vdir.is_dir():
        raise NotFound(f"There is no published version {version}.", hint="See the versions with: easy-ui versions")
    m = _verify_version(vdir)
    wanted = {d["slug"]: _read_version_file(vdir, d) for d in m["dashboards"]}
    ddir = config.dashboards_dir
    for p in (config.app_dir / "ui", ddir):
        if p.is_symlink():
            raise Refused(f"{paths.display_path(p, config.app_dir)} is a link, so easy-ui does not write through it.",
                          hint="Make it a real folder inside the project.", code="link_refused")
    ddir.mkdir(parents=True, exist_ok=True)
    files, _problems, _notes = read_dashboard_dir(ddir, label_prefix="ui/dashboards/")
    moved: list[str] = []
    stamp = _now(now).strftime("%Y%m%d-%H%M%S")
    backup = ddir / f".before-restore-{stamp}"
    for f in files:
        if wanted.get(f.slug) == f.data:
            continue
        backup.mkdir(mode=0o700, exist_ok=True)
        os.replace(ddir / f.name, backup / f.name)
        moved.append(f.name)
    restored = []
    for slug, data in wanted.items():
        target = ddir / f"{slug}.yaml"
        if target.is_symlink():
            target.unlink()
        if target.exists() and target.read_bytes() == data:
            continue
        tmp = ddir / f".{slug}.yaml.restore-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        restored.append(f"{slug}.yaml")
    # The theme, stylesheets and assets of the version, too (the ones there now go to ui/.before-restore-<time>/).
    style_wanted = {b.path: b.data for b in _version_style_files(vdir, m).blobs()}
    style_backup = config.app_dir / "ui" / f".before-restore-{stamp}"
    style_restored, style_moved = styles.restore(config.app_dir / "ui", style_wanted, backup=style_backup)
    restored += style_restored
    return {"app": config.name, "version": version, "restored": restored, "set_aside": moved + style_moved,
            "backup": paths.display_path(backup, config.app_dir) if moved else None,
            "styles_backup": paths.display_path(style_backup, config.app_dir) if style_moved else None}


# ---------------------------------------------------------------- reading for the server

_cache: dict[tuple[str, int, str, str], spec.Dashboard] = {}


def _published_manifest(st: paths.StatePaths) -> tuple[Path, dict[str, Any]] | None:
    vdir = published_dir(st)
    if vdir is None:
        return None
    return vdir, _read_manifest(vdir)


def list_published(where: AppConfig | paths.StatePaths) -> list[dict[str, Any]]:
    got = _published_manifest(_state(where))
    if got is None:
        return []
    return [{"slug": d["slug"], "title": d.get("title") or d["slug"], "description": d.get("description")}
            for d in got[1]["dashboards"]]


def _not_published(slug: str) -> NotFound:
    return NotFound(
        f'There is no published dashboard called "{str(slug)[:60]}".',
        hint="Check the address, or publish the dashboard with: easy-ui publish",
    )


def load_published_dashboard(where: AppConfig | paths.StatePaths, slug: str) -> spec.Dashboard:
    if not isinstance(slug, str) or not spec.SLUG_RE.match(slug):
        raise _not_published(str(slug))
    st = _state(where)
    got = _published_manifest(st)
    if got is None:
        raise NotFound("No dashboards have been published yet, so there is nothing to show.",
                       hint="Publish them with: easy-ui publish", code="nothing_published")
    vdir, manifest = got
    entry = next((d for d in manifest["dashboards"] if d["slug"] == slug), None)
    if entry is None:
        raise _not_published(slug)
    try:
        ino = os.lstat(vdir).st_ino
    except OSError:
        raise _not_published(slug) from None
    key = (str(vdir), ino, slug, entry["sha256"])
    dash = _cache.get(key)
    if dash is None:
        data = _read_version_file(vdir, entry)
        dash = spec.loads(data.decode("utf-8"), slug=slug, file=f"{slug}.yaml")
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = dash
    return dash


def load_published(where: AppConfig | paths.StatePaths, slug: str, *, today: _dt.date | None = None) -> dict[str, Any]:
    return spec.compile_dashboard(load_published_dashboard(where, slug), today=today)


_styles_cache: dict[tuple[str, int, str], styles.StyleSet] = {}


def load_published_styles(where: AppConfig | paths.StatePaths) -> styles.StyleSet:
    """The theme, stylesheets and assets of the published version: every file hash-checked, then checked again.

    ``styles.EMPTY`` when nothing is published or the version has no style files. Raises ``PublishedDamaged``.
    """
    got = _published_manifest(_state(where))
    if got is None:
        return styles.EMPTY
    vdir, manifest = got
    entries = manifest.get("styles") or []
    if not entries:
        return styles.EMPTY
    try:
        ino = os.lstat(vdir).st_ino
    except OSError:
        return styles.EMPTY
    key = (str(vdir), ino, hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest())
    cached = _styles_cache.get(key)
    if cached is not None:
        return cached
    checked = styles.check(_version_style_files(vdir, manifest))
    if checked.styles is None:
        first = checked.errors[0]
        raise PublishedDamaged(f"The published look (version {vdir.name}) does not pass easy-ui's checks: "
                               f"{first['problem']}", hint="Publish again with: easy-ui publish")
    if len(_styles_cache) >= 8:
        _styles_cache.clear()
    _styles_cache[key] = checked.styles
    return checked.styles


class PublishedSource:
    """Where the server gets dashboards from: ``specs/current`` (the published version).

    The preview uses ``easyui.preview.WorkingSource`` with the same methods, so
    the server only needs to talk to "a source":

    - ``list() -> [{"slug", "title", "description"}]``
    - ``compiled(slug, *, today=None) -> dict``  for ``GET /api/dashboards/{slug}``
    - ``dashboard(slug) -> spec.Dashboard``       for ``spec.run_queries``
    - ``bundle_path() -> Path``                   the data bundle queries run on (raises ``NoData``)
    - ``rev() -> str``                            for ``GET /api/rev``
    - ``styles() -> styles.StyleSet``             the theme, stylesheets and assets to serve
    """

    preview = False

    def __init__(self, where: AppConfig | paths.StatePaths) -> None:
        self.where = where

    def list(self) -> list[dict[str, Any]]:
        return list_published(self.where)

    def dashboard(self, slug: str) -> spec.Dashboard:
        return load_published_dashboard(self.where, slug)

    def compiled(self, slug: str, *, today: _dt.date | None = None) -> dict[str, Any]:
        return load_published(self.where, slug, today=today)

    def bundle_path(self) -> Path:
        return bundle.require_current(self.where)

    def rev(self) -> str:
        return bundle.rev(self.where)

    def styles(self) -> styles.StyleSet:
        return load_published_styles(self.where)


# ---------------------------------------------------------------- command line


def _warn_lines(warnings: list[dict[str, Any]]) -> list[str]:
    lines = []
    for w in warnings[:20]:
        p = spec.Problem(w["file"], w.get("where", ""), w["problem"], w.get("line"), w.get("hint"), level="warning",
                         place=w.get("place"))
        lines.append("  warning: " + p.text())
    if len(warnings) > 20:
        lines.append(f"  ... and {len(warnings) - 20} more warning(s); see them with: easy-ui validate")
    return lines


def _cmd_publish(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config

    cfg = load_config(args)
    res = publish(cfg, note=args.note, keep=args.keep)
    slugs = ", ".join(d["slug"] for d in res["dashboards"])
    if res["unchanged"]:
        text = (f"Nothing changed since version {res['version']} ({slugs}), so no new version was made. "
                "The server keeps showing it.")
    else:
        n = len(res["dashboards"])
        look = _look_text(res.get("styles") or [])
        text = (f"Published version {res['version']} with {n} dashboard{'s' if n != 1 else ''} ({slugs}){look}. "
                "Open pages show it on their next refresh. To undo: easy-ui rollback")
    lines = [text, *_warn_lines(res["warnings"]), *res["notes"]]
    return Result(res, "\n".join(lines))


def _look_text(entries: list[dict[str, Any]]) -> str:
    parts = []
    if any(e["path"] == "theme.yaml" for e in entries):
        parts.append("the theme")
    n = sum(1 for e in entries if e["path"].startswith("styles/"))
    if n:
        parts.append(f"{n} stylesheet{'s' if n != 1 else ''}")
    n = sum(1 for e in entries if e["path"].startswith("assets/"))
    if n:
        parts.append(f"{n} font or picture file{'s' if n != 1 else ''}")
    return (", with " + " and ".join(parts)) if parts else ""


def _cmd_versions(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config

    cfg = load_config(args)
    res = list_versions(cfg)
    if not res["versions"]:
        return Result(res, "Nothing has been published yet. Publish with: easy-ui publish")
    lines = ["Published versions (newest first; * is the one shown now):"]
    for v in res["versions"]:
        mark = "* " if v["current"] else "  "
        if not v["ok"]:
            lines.append(f"{mark}{v['version']:>4}  damaged: {v['problem']}")
            continue
        note = f"  \"{v['note']}\"" if v.get("note") else ""
        lines.append(f"{mark}{v['version']:>4}  {v['published_at']}  {', '.join(v['dashboards'])}{note}")
    lines.append("Go back with: easy-ui rollback [version]")
    return Result(res, "\n".join(lines))


def _cmd_rollback(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config

    cfg = load_config(args)
    res = rollback(cfg, args.version)
    if not res["changed"]:
        text = f"Version {res['version']} is already the one shown, so nothing was changed."
    else:
        text = f"Now showing version {res['version']}. Open pages show it on their next refresh."
        if res["previous"] is not None:
            text += f" It was version {res['previous']}; to go back to it: easy-ui rollback {res['previous']}"
    if res["skipped"]:
        text += "\nSkipped damaged versions: " + "; ".join(res["skipped"])
    if args.restore_files:
        restored = restore_files(cfg, res["version"])
        res["files"] = restored
        text += (f"\nThe files in ui/dashboards/ (and the theme, ui/styles/ and ui/assets/) are now those of version "
                 f"{res['version']}, so you can edit from there.")
        if restored["set_aside"]:
            where = " and ".join(x for x in (restored["backup"], restored.get("styles_backup")) if x)
            text += (f" The files that were there before ({', '.join(restored['set_aside'][:8])}) were moved to "
                     f"{where}.")
    else:
        same = working_matches(cfg, res["version"])
        res["files_match"] = same
        if not same:
            text += (f"\nYour files in ui/dashboards/ still hold other dashboards than version {res['version']}: "
                     "running easy-ui publish would show them again. To edit from version "
                     f"{res['version']} instead, run: easy-ui rollback {res['version']} --restore-files")
    return Result(res, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "publish",
        help="Publish the dashboards so the server shows them.",
        description="Check every dashboard in ui/dashboards/ against the current data and, when all are valid, "
        "store them as a new numbered version that the server shows. Nothing changes if anything is wrong.",
    )
    p.add_argument("--note", help=f"a short note saved with the version (up to {MAX_NOTE_LENGTH} characters)")
    p.add_argument("--keep", type=int, default=KEEP_VERSIONS, help=f"how many versions to keep (default {KEEP_VERSIONS})")
    p.set_defaults(func=_cmd_publish)

    p = subparsers.add_parser("versions", help="List the published versions.",
                              description="List the published dashboard versions, newest first.")
    p.set_defaults(func=_cmd_versions)

    p = subparsers.add_parser("rollback", help="Go back to an earlier published version.",
                              description="Switch the dashboards the server shows to an earlier version, instantly. "
                              "Without a number: the version before the current one.")
    p.add_argument("version", nargs="?", help="the version number (see: easy-ui versions)")
    p.add_argument(
        "--restore-files",
        action="store_true",
        help="also copy that version's files back into ui/dashboards/ (the files there now are kept in "
        "ui/dashboards/.before-restore-<time>/)",
    )
    p.set_defaults(func=_cmd_rollback)

    from easyui import preview, units

    preview.register(subparsers)
    units.register(subparsers)
