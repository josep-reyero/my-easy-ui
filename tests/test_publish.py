"""Publishing, versions, rollback, reading published dashboards, and hostile archives."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
from pathlib import Path

import pytest
from conftest import ACTIVITIES_YAML, OVERVIEW_YAML, run_cli, run_cli_json

from easyui import bundle, compose, paths, publish, spec
from easyui.errors import (
    NoData,
    NotFound,
    Refused,
    SpecError,
    TemporaryFailure,
    UsageError,
)

TEXT_ONLY = "spec: easy-ui/v1\ntitle: {title}\nlayout:\n  - - type: text\n      body: Hello {title}.\n"
BAD_KEY = "spec: easy-ui/v1\ntitle: Bad\nlayout:\n  - - type: text\n      bodyy: oops\n"
BAD_COLUMN = OVERVIEW_YAML.replace("value: distance\n", "value: no_such_column\n")
BAD_TABLE = TEXT_ONLY.format(title="T").replace(
    "layout:", "queries:\n  q:\n    sql: SELECT * FROM private_notes\nlayout:"
).replace("  - - type: text\n      body: Hello T.\n", "  - - type: table\n      title: Notes\n      query: q\n")


def specs_dir(app) -> Path:
    return app.config().state.specs


def version_files(app) -> list[str]:
    root = specs_dir(app)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def tamper(path: Path, text: str) -> None:
    os.chmod(path.parent, 0o755)
    os.chmod(path, 0o644)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- publish


def test_publish_needs_data(demo_app):
    with pytest.raises(NoData):
        publish.publish(demo_app.config())
    code, data = run_cli_json("publish")
    assert code == 3 and data["error"]["code"] == "no_data"
    assert publish.current_version(demo_app.config()) is None


def test_publish_creates_immutable_version(built_app):
    cfg = built_app.config()
    t = dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.UTC)
    res = publish.publish(cfg, note="first try", now=t)
    assert res["published"] and not res["unchanged"]
    assert res["version"] == 1 and res["previous"] is None
    assert [d["slug"] for d in res["dashboards"]] == ["activities", "overview"]
    st = cfg.state
    assert os.readlink(st.specs / "current") == "versions/1"
    vdir = st.specs / "versions" / "1"
    m = json.loads((vdir / "manifest.json").read_text())
    assert m["version"] == 1 and m["note"] == "first try" and m["published_at"] == "2026-05-01T12:00:00Z"
    assert m["app"] == "demo" and m["spec"] == spec.SPEC_VERSION and m["source"] == "working folder"
    assert m["bundle"] == bundle.require_current(cfg).stem
    for d in m["dashboards"]:
        f = vdir / d["file"]
        assert hashlib.sha256(f.read_bytes()).hexdigest() == d["sha256"]
        assert d["size"] == f.stat().st_size
        assert stat.S_IMODE(f.stat().st_mode) == 0o444
    # writable folders: deleting the state folder to start over must just work
    assert stat.S_IMODE(vdir.stat().st_mode) == 0o755
    assert stat.S_IMODE((vdir / "dashboards").stat().st_mode) == 0o755
    # A dashboard made only by the project is stored as it is; the overview is stored composed with its apps' sections.
    assert (vdir / "dashboards" / "activities.yaml").read_text() == ACTIVITIES_YAML
    composed = (vdir / "dashboards" / "overview.yaml").read_text()
    assert composed.startswith("# Composed by easy-ui from ui/dashboards/overview.yaml, apps/demo_moves/")
    assert "app: demo-moves" in composed and "app: demo-sleep" in composed and "anchor:" not in composed
    over = next(d for d in m["dashboards"] if d["slug"] == "overview")
    assert over["from"] == "project" and over["replaces"] is None
    assert [(s["app"], s["id"], s["placed"]) for s in over["sections"]] == [
        ("demo-moves", "kinds", "after:movement"), ("demo-sleep", "sleep", "end")]
    assert [x["from"] for x in over["sources"]] == ["project", "demo-moves", "demo-sleep"]
    # The project's own files are kept as they were, for rollback --restore-files.
    assert sorted(x["path"] for x in m["project_files"]) == ["ui/dashboards/activities.yaml", "ui/dashboards/overview.yaml"]
    assert (vdir / "project" / "ui" / "dashboards" / "overview.yaml").read_text() == OVERVIEW_YAML
    assert not [p for p in st.specs.iterdir() if p.name.startswith(".tmp-")]


def test_publish_unchanged_makes_no_new_version(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    before = bundle.rev(cfg)
    res = publish.publish(cfg, note="again")
    assert res["unchanged"] and not res["published"] and res["version"] == 1
    assert publish._version_numbers(cfg.state) == [1]
    assert bundle.rev(cfg) == before


def test_publish_change_makes_next_version_and_changes_rev(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    r1 = bundle.rev(cfg)
    built_app.write_dashboard("extra", TEXT_ONLY.format(title="Extra"))
    res = publish.publish(cfg)
    assert res["version"] == 2 and res["previous"] == 1
    assert bundle.rev(cfg) != r1
    assert [d["slug"] for d in publish.list_published(cfg)] == ["activities", "extra", "overview"]


@pytest.mark.parametrize("text,needle", [(BAD_KEY, "bodyy"), (BAD_COLUMN, "no_such_column"), (BAD_TABLE, "private_notes")])
def test_invalid_dashboard_switches_nothing(built_app, text, needle):
    cfg = built_app.config()
    publish.publish(cfg)
    files_before = version_files(built_app)
    built_app.write_dashboard("broken", text)
    with pytest.raises(SpecError) as ei:
        publish.publish(cfg)
    e = ei.value
    assert "ui/dashboards/broken.yaml" in e.message and "Nothing was published" in e.message
    assert any(needle in json.dumps(p) for p in e.problems)
    assert all(p["file"] == "ui/dashboards/broken.yaml" for p in e.problems)
    assert publish.current_version(cfg) == 1
    assert version_files(built_app) == files_before
    code, data = run_cli_json("publish")
    assert code == 1 and data["error"]["code"] == "spec_invalid" and data["error"]["problems"]


def test_one_bad_file_blocks_the_good_ones(built_app):
    built_app.write_dashboard("broken", BAD_KEY)
    with pytest.raises(SpecError):
        publish.publish(built_app.config())
    assert publish.current_version(built_app.config()) is None
    assert not (specs_dir(built_app) / "versions").exists() or not list((specs_dir(built_app) / "versions").iterdir())


def test_no_dashboards(built_app):
    for p in built_app.config().dashboards_dir.iterdir():
        p.unlink()
    with pytest.raises(NotFound) as ei:
        publish.publish(built_app.config())
    assert ei.value.code == "no_dashboards"


def test_warnings_are_returned_but_do_not_block(built_app):
    built_app.write_dashboard("unused", TEXT_ONLY.format(title="U").replace(
        "layout:", "queries:\n  spare:\n    sql: SELECT 1 AS one\nlayout:"))
    res = publish.publish(built_app.config())
    assert res["published"] and any("spare" in json.dumps(w) for w in res["warnings"])


# ---------------------------------------------------------------- hardened file handling


def test_symlinked_dashboard_is_refused(built_app, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text(TEXT_ONLY.format(title="Outside"))
    os.symlink(outside, built_app.config().dashboards_dir / "linked.yaml")
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "link" in ei.value.message and "linked.yaml" in ei.value.message
    assert publish.current_version(built_app.config()) is None


def test_symlinked_dashboards_folder_is_refused(built_app, tmp_path):
    cfg = built_app.config()
    real = tmp_path / "elsewhere"
    cfg.dashboards_dir.rename(real)
    os.symlink(real, cfg.dashboards_dir)
    with pytest.raises(SpecError) as ei:
        publish.publish(cfg)
    assert "link" in ei.value.message


def test_dangling_symlink_is_refused(built_app):
    os.symlink("/nonexistent/x.yaml", built_app.config().dashboards_dir / "ghost.yaml")
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "ghost.yaml" in ei.value.message


def test_fifo_is_refused_without_hanging(built_app):
    os.mkfifo(built_app.config().dashboards_dir / "pipe.yaml")
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "not a plain file" in ei.value.message


def test_folder_named_like_a_dashboard_is_refused(built_app):
    (built_app.config().dashboards_dir / "dir.yaml").mkdir()
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "not a plain file" in ei.value.message


def test_oversized_file_is_refused(built_app):
    big = TEXT_ONLY.format(title="Big") + "#" + "x" * (publish.MAX_FILE_BYTES + 10) + "\n"
    built_app.write_dashboard("big", big)
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "bytes" in ei.value.message


@pytest.mark.parametrize("name", ["Upper.yaml", "with space.yaml", "-dash.yaml", "under_score.yaml",
                                  "x" * 42 + ".yaml", "dots.v2.yaml"])
def test_bad_file_names_are_refused(built_app, name):
    (built_app.config().dashboards_dir / name).write_text(TEXT_ONLY.format(title="T"))
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "cannot be used" in ei.value.message


def test_too_many_dashboards(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_DASHBOARDS", 3)
    for i in range(3):
        built_app.write_dashboard(f"extra-{i}", TEXT_ONLY.format(title=f"E{i}"))
    with pytest.raises(Refused) as ei:
        publish.publish(built_app.config())
    assert ei.value.code == "too_many_dashboards"
    code, data = run_cli_json("publish")
    assert code == 5


def test_dotfiles_and_other_files_are_ignored(built_app):
    d = built_app.config().dashboards_dir
    os.symlink("nowhere", d / ".#overview.yaml")  # emacs lock file
    (d / ".overview.yaml.swp").write_bytes(b"\0\1")
    (d / "README.md").write_text("notes")
    (d / "old.yml").write_text(TEXT_ONLY.format(title="Old"))
    res = publish.publish(built_app.config())
    assert [x["slug"] for x in res["dashboards"]] == ["activities", "overview"]
    assert any(".yml" in n for n in res["notes"])


def test_non_utf8_is_refused(built_app):
    (built_app.config().dashboards_dir / "latin.yaml").write_bytes("title: caf\xe9\n".encode("latin-1"))
    with pytest.raises(SpecError) as ei:
        publish.publish(built_app.config())
    assert "UTF-8" in ei.value.message


@pytest.mark.parametrize("note", ["two\nlines", "x" * 201, "tab\there", 5])
def test_bad_note(built_app, note):
    with pytest.raises(UsageError):
        publish.publish(built_app.config(), note=note)


def test_bad_note_cli_exit_code(built_app):
    code, data = run_cli_json("publish", "--note", "a\nb")
    assert code == 2 and data["error"]["code"] == "usage"


def test_lock_blocks_a_second_publish(built_app):
    import fcntl

    st = built_app.config().state.ensure()
    fh = open(st.specs / ".publish.lock", "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TemporaryFailure):
            publish.publish(built_app.config())
        code, _ = run_cli_json("publish")
        assert code == 75
    finally:
        fh.close()
    assert publish.publish(built_app.config())["version"] == 1


def test_concurrent_publishes_make_distinct_versions(built_app):
    cfg = built_app.config()
    results, errors = [], []

    def go(i):
        try:
            built = TEXT_ONLY.format(title=f"T{i}")
            results.append(publish._publish_files(
                cfg.state, cfg.name, [publish.SourceFile(f"t{i}", f"t{i}.yaml", f"t{i}.yaml", built.encode(),
                                                         hashlib.sha256(built.encode()).hexdigest())],
                [], note=None, keep=10, now=None, source="test", notes=[]))
        except TemporaryFailure as e:
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    versions = [r["version"] for r in results]
    assert len(versions) == len(set(versions)) and len(results) + len(errors) == 4


# ---------------------------------------------------------------- keeping 10, versions, rollback


def publish_n(app, n):
    out = []
    for i in range(n):
        app.write_dashboard("counter", TEXT_ONLY.format(title=f"Counter {i}"))
        out.append(publish.publish(app.config(), note=f"v{i}")["version"])
    return out


def test_keeps_last_ten(built_app):
    cfg = built_app.config()
    assert publish_n(built_app, 12) == list(range(1, 13))
    assert publish._version_numbers(cfg.state) == list(range(3, 13))
    listing = publish.list_versions(cfg)
    assert [v["version"] for v in listing["versions"]] == list(range(12, 2, -1))
    assert listing["current"] == 12 and listing["versions"][0]["current"]


def test_prune_never_removes_current(built_app):
    cfg = built_app.config()
    for i in range(12):
        built_app.write_dashboard("counter", TEXT_ONLY.format(title=f"Counter {i}"))
        publish.publish(cfg, keep=100)
    publish.rollback(cfg, 1)
    removed = publish._prune(cfg.state, 10)
    assert removed == [2]
    nums = publish._version_numbers(cfg.state)
    assert nums == [1, *range(3, 13)] and publish.current_version(cfg) == 1
    assert publish.load_published(cfg, "counter")["title"] == "Counter 0"


def test_keep_option(built_app):
    publish_n(built_app, 3)
    built_app.write_dashboard("counter", TEXT_ONLY.format(title="Last"))
    res = publish.publish(built_app.config(), keep=1)
    assert publish._version_numbers(built_app.config().state) == [4]
    assert sorted(res["pruned"]) == [1, 2, 3]
    with pytest.raises(UsageError):
        publish.publish(built_app.config(), keep=0)


def test_rollback_previous_and_forward(built_app):
    cfg = built_app.config()
    publish_n(built_app, 3)
    res = publish.rollback(cfg)
    assert res == {**res, "version": 2, "previous": 3, "changed": True}
    assert publish.current_version(cfg) == 2
    assert publish.load_published(cfg, "counter")["title"] == "Counter 1"
    assert publish.rollback(cfg)["version"] == 1
    with pytest.raises(NotFound):
        publish.rollback(cfg)
    assert publish.rollback(cfg, "3")["version"] == 3
    again = publish.rollback(cfg, 3)
    assert again["changed"] is False


def test_rollback_changes_rev(built_app):
    cfg = built_app.config()
    publish_n(built_app, 2)
    r = bundle.rev(cfg)
    publish.rollback(cfg)
    assert bundle.rev(cfg) != r


def test_rollback_errors(built_app):
    cfg = built_app.config()
    with pytest.raises(NotFound):
        publish.rollback(cfg)
    publish_n(built_app, 2)
    with pytest.raises(NotFound):
        publish.rollback(cfg, 99)
    for bad in ("abc", "0", "-1", "../1", "1; rm", True):
        with pytest.raises(UsageError):
            publish.rollback(cfg, bad)
    assert run_cli_json("rollback", "99")[0] == 3
    assert run_cli_json("rollback", "abc")[0] == 2


def test_rollback_refuses_damaged_version(built_app):
    cfg = built_app.config()
    publish_n(built_app, 2)
    tamper(cfg.state.specs / "versions" / "1" / "dashboards" / "counter.yaml", TEXT_ONLY.format(title="Evil"))
    with pytest.raises(Refused) as ei:
        publish.rollback(cfg, 1)
    assert ei.value.code == "version_damaged"
    assert publish.current_version(cfg) == 2
    with pytest.raises(NotFound):
        publish.rollback(cfg)  # the only older one is damaged


def test_rollback_skips_damaged_previous(built_app):
    cfg = built_app.config()
    publish_n(built_app, 3)
    (cfg.state.specs / "versions" / "2").chmod(0o755)
    (cfg.state.specs / "versions" / "2" / "manifest.json").chmod(0o644)
    (cfg.state.specs / "versions" / "2" / "manifest.json").write_text("{not json")
    res = publish.rollback(cfg)
    assert res["version"] == 1 and res["skipped"]
    listing = publish.list_versions(cfg)
    assert [v["ok"] for v in listing["versions"]] == [True, False, True]


def test_versions_cli(built_app):
    code, out, _ = run_cli("versions")
    assert code == 0 and "Nothing has been published" in out
    publish_n(built_app, 2)
    code, data = run_cli_json("versions")
    assert code == 0 and data["current"] == 2
    assert data["versions"][0]["note"] == "v1" and data["versions"][0]["dashboards"] == ["activities", "counter", "overview"]
    code, out, _ = run_cli("versions")
    assert "* " in out and "rollback" in out


def test_publish_and_rollback_cli(built_app):
    code, data = run_cli_json("publish", "--note", "hello")
    assert code == 0 and data["version"] == 1 and data["note"] == "hello"
    code, out, _ = run_cli("--app-dir", str(built_app.app_dir), "publish")
    assert code == 0 and "Nothing changed" in out
    built_app.write_dashboard("extra", TEXT_ONLY.format(title="Extra"))
    code, out, _ = run_cli("publish", "--json")
    assert code == 0 and json.loads(out)["version"] == 2
    code, out, _ = run_cli("rollback")
    assert code == 0 and "Now showing version 1" in out
    code, data = run_cli_json("rollback", "2")
    assert code == 0 and data["version"] == 2 and data["previous"] == 1


# ---------------------------------------------------------------- reading published dashboards


def test_list_and_load_published(built_app):
    cfg = built_app.config()
    assert publish.list_published(cfg) == []
    with pytest.raises(NotFound) as ei:
        publish.load_published(cfg, "overview")
    assert ei.value.code == "nothing_published"
    publish.publish(cfg)
    listing = publish.list_published(cfg)
    assert listing[1] == {"slug": "overview", "title": "Overview",
                          "description": "Daily movement at a glance (synthetic demo data)."}
    compiled = publish.load_published(cfg, "overview", today=dt.date(2026, 5, 1))
    composed = compose.compose(cfg).get("overview").dashboard
    assert compiled == spec.compile_dashboard(composed, today=dt.date(2026, 5, 1))
    assert "SELECT" not in json.dumps(compiled) and "FROM days" not in json.dumps(compiled)  # no SQL text
    dash = publish.load_published_dashboard(cfg, "activities")
    res = spec.run_queries(dash, ["by_kind"], {"kind": "run"}, bundle_path=bundle.require_current(cfg))
    assert res["results"]["by_kind"]["columns"] == ["kind", "Count", "Distance"]
    assert publish.load_published_dashboard(cfg, "activities") is dash  # cached


@pytest.mark.parametrize("slug", ["nope", "../versions", "OVERVIEW", "", "a/b", "overview.yaml", None, 7])
def test_load_published_unknown(built_app, slug):
    publish.publish(built_app.config())
    with pytest.raises(NotFound):
        publish.load_published(built_app.config(), slug)


def test_working_folder_changes_do_not_leak_into_published(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    built_app.write_dashboard("overview", OVERVIEW_YAML.replace("title: Overview", "title: Draft"))
    built_app.write_dashboard("draft", TEXT_ONLY.format(title="Draft"))
    assert publish.load_published(cfg, "overview")["title"] == "Overview"
    assert "draft" not in [d["slug"] for d in publish.list_published(cfg)]


def test_tampered_published_file_is_not_served(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    publish._cache.clear()
    f = cfg.state.specs / "versions" / "1" / "dashboards" / "overview.yaml"
    tamper(f, OVERVIEW_YAML.replace("title: Overview", "title: Tampered"))
    with pytest.raises(publish.PublishedDamaged) as ei:
        publish.load_published(cfg, "overview")
    assert ei.value.http_status == 500 and "changed or damaged" in ei.value.message


def test_symlink_swapped_into_published_version_is_not_followed(built_app, tmp_path):
    cfg = built_app.config()
    publish.publish(cfg)
    publish._cache.clear()
    ddir = cfg.state.specs / "versions" / "1" / "dashboards"
    os.chmod(ddir, 0o755)
    evil = tmp_path / "evil.yaml"
    evil.write_text(OVERVIEW_YAML)  # same content: the hash would match if the link were followed
    (ddir / "overview.yaml").unlink()
    os.symlink(evil, ddir / "overview.yaml")
    with pytest.raises(publish.PublishedDamaged):
        publish.load_published(cfg, "overview")


@pytest.mark.parametrize("target", ["/etc", "../../..", "versions/../../x", "versions/1/..", "versions/01", "versions/abc"])
def test_odd_current_link_is_ignored(built_app, target):
    cfg = built_app.config()
    publish.publish(cfg)
    link = cfg.state.specs / "current"
    link.unlink()
    os.symlink(target, link)
    assert publish.current_version(cfg) is None
    assert publish.list_published(cfg) == []


def test_current_link_to_symlinked_version_is_ignored(built_app, tmp_path):
    cfg = built_app.config()
    publish.publish(cfg)
    vdir = cfg.state.specs / "versions"
    os.symlink(tmp_path, vdir / "5")
    link = cfg.state.specs / "current"
    link.unlink()
    os.symlink("versions/5", link)
    assert publish.current_version(cfg) is None


def test_published_source(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    src = publish.PublishedSource(cfg)
    assert [d["slug"] for d in src.list()] == ["activities", "overview"]
    assert src.compiled("overview")["slug"] == "overview"
    assert src.dashboard("overview").slug == "overview"
    assert src.bundle_path() == bundle.require_current(cfg)
    assert src.rev() == bundle.rev(cfg)
    assert src.preview is False
    # also works from StatePaths alone (the server may only have the state folder)
    st = paths.state_paths("demo")
    assert publish.PublishedSource(st).list() == src.list()


# ---------------------------------------------------------------- archives


def make_tar(entries, *, gz=False, fmt=tarfile.PAX_FORMAT) -> bytes:
    """entries: list of (name, bytes | None, type, extra)"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w", format=fmt) as tf:
        for e in entries:
            name, data, kind = e[0], e[1], (e[2] if len(e) > 2 else tarfile.REGTYPE)
            info = tarfile.TarInfo(name)
            info.type = kind
            if len(e) > 3:
                for k, v in e[3].items():
                    setattr(info, k, v)
            if data is not None:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.addfile(info)
    return buf.getvalue()


def good_entries(prefix=""):
    return [(f"{prefix}overview.yaml", OVERVIEW_YAML.encode()), (f"{prefix}activities.yaml", ACTIVITIES_YAML.encode())]


@pytest.mark.parametrize("prefix", ["", "./", "dashboards/", "./dashboards/", "ui/dashboards/"])
@pytest.mark.parametrize("gz", [False, True])
def test_archive_publish(built_app, prefix, gz):
    cfg = built_app.config()
    raw = make_tar(good_entries(prefix), gz=gz)
    res = publish.publish_from_archive(cfg, io.BytesIO(raw), note="remote")
    assert res["published"] and res["version"] == 1 and res["source"] == "archive"
    assert [d["slug"] for d in publish.list_published(cfg)] == ["activities", "overview"]
    m = json.loads((cfg.state.specs / "versions" / "1" / "manifest.json").read_text())
    assert m["source"] == "archive" and m["note"] == "remote"


def test_archive_with_directory_entries_and_bytes_input(built_app):
    raw = make_tar([("dashboards", None, tarfile.DIRTYPE), *good_entries("dashboards/")])
    res = publish.publish_from_archive(paths.state_paths("demo"), raw)
    assert res["version"] == 1 and res["app"] == "demo"


def test_archive_invalid_dashboard_switches_nothing(built_app):
    cfg = built_app.config()
    publish.publish(cfg)
    before = version_files(built_app)
    raw = make_tar([*good_entries(), ("broken.yaml", BAD_COLUMN.encode())])
    with pytest.raises(SpecError) as ei:
        publish.publish_from_archive(cfg, raw)
    assert "archive:broken.yaml" in ei.value.message
    assert version_files(built_app) == before and publish.current_version(cfg) == 1


HOSTILE = {
    "absolute": [("/etc/evil.yaml", b"x")],
    "dotdot": [("../evil.yaml", b"x")],
    "dotdot-inner": [("dashboards/../../evil.yaml", b"x")],
    "backslash": [("..\\evil.yaml", b"x")],
    "double-slash": [("dashboards//evil.yaml", b"x")],
    "dot-inner": [("dashboards/./evil.yaml", b"x")],
    "drive": [("C:/evil.yaml", b"x")],
    "symlink": [("overview.yaml", None, tarfile.SYMTYPE, {"linkname": "/etc/passwd"})],
    "hardlink": [("overview.yaml", None, tarfile.LNKTYPE, {"linkname": "activities.yaml"})],
    "chardev": [("overview.yaml", None, tarfile.CHRTYPE, {"devmajor": 1, "devminor": 3})],
    "blockdev": [("overview.yaml", None, tarfile.BLKTYPE)],
    "fifo": [("overview.yaml", None, tarfile.FIFOTYPE)],
    "contiguous": [("overview.yaml", b"x", tarfile.CONTTYPE)],
    "other-dir": [("secrets", None, tarfile.DIRTYPE)],
    "nested-dir-file": [("ui/other/overview.yaml", b"x")],
    "not-yaml": [("run.sh", b"#!/bin/sh\n")],
    "yml": [("overview.yml", b"x")],
    "manifest": [("manifest.json", b"{}")],
    "bad-slug": [("Overview.yaml", b"x")],
    "bad-slug-space": [("my dash.yaml", b"x")],
    "hidden": [(".overview.yaml", b"x")],
    "duplicate": [("overview.yaml", OVERVIEW_YAML.encode()), ("dashboards/overview.yaml", b"spec: evil")],
    "duplicate-same-path": [("overview.yaml", OVERVIEW_YAML.encode()), ("overview.yaml", b"spec: evil")],
    "oversize": [("overview.yaml", b"#" * (publish.MAX_FILE_BYTES + 1))],
    "nul-name": [("over\x00view.yaml", b"x")],
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_hostile_archives_are_refused(built_app, tmp_path, case):
    cfg = built_app.config()
    entries = HOSTILE[case]
    if case == "nul-name":
        raw = make_tar([("overview.yaml", b"x")], fmt=tarfile.USTAR_FORMAT).replace(b"overview.yaml", b"over\x00iew.yaml", 1)
    else:
        raw = make_tar(entries)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises((Refused, SpecError)) as ei:
        publish.publish_from_archive(cfg, raw)
    if not isinstance(ei.value, SpecError):
        assert ei.value.code == "archive_refused" and ei.value.exit_code == 5
        assert "Nothing was published" in ei.value.message
    assert publish.current_version(cfg) is None
    assert not (cfg.state.specs / "versions").exists() or not any((cfg.state.specs / "versions").iterdir())
    after = sorted(str(p) for p in tmp_path.rglob("*"))
    new = [p for p in after if p not in before and "/state/" not in p]
    assert new == []  # nothing was written anywhere else
    assert not (tmp_path / "evil.yaml").exists() and not Path("/etc/evil.yaml").exists()


def test_pax_header_cannot_rename_into_parent(built_app):
    raw = make_tar([("overview.yaml", OVERVIEW_YAML.encode(), tarfile.REGTYPE, {"pax_headers": {"path": "../../evil.yaml"}})])
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_pax_linkpath_on_regular_file_is_refused(built_app):
    raw = make_tar([("overview.yaml", OVERVIEW_YAML.encode(), tarfile.REGTYPE, {"pax_headers": {"linkpath": "/etc/passwd"}})])
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_too_many_members(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_ARCHIVE_MEMBERS", 5)
    raw = make_tar([(f"d{i}.yaml", TEXT_ONLY.format(title=f"D{i}").encode()) for i in range(6)])
    with pytest.raises(Refused) as ei:
        publish.publish_from_archive(built_app.config(), raw)
    assert "entries" in ei.value.message


def test_too_many_dashboards_in_archive(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_DASHBOARDS", 2)
    raw = make_tar([(f"d{i}.yaml", TEXT_ONLY.format(title=f"D{i}").encode()) for i in range(3)])
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_gzip_bomb_is_refused(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_ARCHIVE_UNPACKED_BYTES", 2_000_000)
    import gzip

    raw = gzip.compress(b"\0" * 5_000_000)
    assert len(raw) < 100_000
    with pytest.raises(Refused) as ei:
        publish.publish_from_archive(built_app.config(), raw)
    assert "unpacks to more than" in ei.value.message


def test_huge_pax_header_inside_limit_is_bounded(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_ARCHIVE_UNPACKED_BYTES", 1_000_000)
    raw = make_tar([("overview.yaml", OVERVIEW_YAML.encode(), tarfile.REGTYPE, {"pax_headers": {"comment": "x" * 3_000_000}})],
                   gz=True)
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_archive_too_big_raw(built_app, monkeypatch):
    monkeypatch.setattr(publish, "MAX_ARCHIVE_BYTES", 1000)

    class Endless(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            b[: len(b)] = b"\0" * len(b)
            return len(b)

    with pytest.raises(Refused) as ei:
        publish.publish_from_archive(built_app.config(), Endless())
    assert "larger than" in ei.value.message


@pytest.mark.parametrize("raw", [b"", b"not a tar file at all" * 10, b"\x1f\x8bgarbage", b"\0" * 1024])
def test_garbage_archives(built_app, raw):
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_gzip_with_trailing_data_is_refused(built_app):
    raw = make_tar(good_entries(), gz=True) + b"TRAILING"
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw)


def test_truncated_member_is_refused(built_app):
    raw = make_tar(good_entries())
    with pytest.raises(Refused):
        publish.publish_from_archive(built_app.config(), raw[:700])


def test_archive_validates_note(built_app):
    with pytest.raises(UsageError):
        publish.publish_from_archive(built_app.config(), make_tar(good_entries()), note="a\nb")


def test_archive_without_data(demo_app):
    with pytest.raises(NoData):
        publish.publish_from_archive(demo_app.config(), make_tar(good_entries()))
