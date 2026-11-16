"""easy-ui export: the static copy holds only what the chosen dashboards show, and nothing leaks."""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from easyui import auth, export, publish
from easyui.errors import EasyUIError, NotFound, Refused, UsageError

TODAY = dt.date(2026, 5, 1)
DATA_RE = re.compile(r'<script type="application/json" id="easyui-export">(.*?)</script>', re.S)


@pytest.fixture
def published(built_app):
    publish.publish(built_app.config())
    auth.set_password(built_app.config().state.auth, "a long synthetic password")
    return built_app


def payload_of(out: Path) -> dict:
    return json.loads(DATA_RE.search((out / "index.html").read_text()).group(1))


def all_text(out: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in sorted(out.rglob("*")) if p.is_file())


def sql_fragments(app_dir: Path) -> list[str]:
    """Distinctive pieces of every query in the dashboard files."""
    out = []
    for f in (app_dir / "ui" / "dashboards").glob("*.yaml"):
        for m in re.finditer(r"(SELECT [^\n]{10,40}|FROM [a-z_]+ WHERE[^\n]{0,20}|GROUP BY [a-z]+)", f.read_text()):
            out.append(m.group(1))
    assert out
    return out


def test_plain_export_contents(published, tmp_path):
    out = tmp_path / "site"
    res = export.export(published.config(), out=out, today=TODAY)
    assert res["files"] == sorted([
        ".easyui-export", "assets/easyui-export.js", "assets/icon.svg", "assets/style.css",
        "assets/vendor/echarts.LICENSE.txt", "assets/vendor/echarts.NOTICE.txt", "assets/vendor/echarts.min.js",
        "index.html", "ui/apps/demo-sleep/styles/sleep.css",
    ])
    assert res["look"] == ["ui/apps/demo-sleep/styles/sleep.css"]  # the sleep app's stylesheet, scoped to its part
    assert b'[data-app="demo-sleep"]' in (out / "ui/apps/demo-sleep/styles/sleep.css").read_bytes()
    assert res["dashboards"] == ["overview", "activities"] and res["fragment"] is None
    p = payload_of(out)
    assert set(p["specs"]) == set(p["data"]) == {"overview", "activities"}
    assert p["data"]["overview"]["values"] == {"since": "2026-01-01", "period": "day"}
    assert all(s["fixed"] for s in p["specs"].values())
    rows = p["data"]["activities"]["results"]["recent"]["rows"]
    assert rows and len(rows) == res["visible"][1]["queries"][0]["rows"]


def test_export_never_carries_sql_sign_in_or_other_tables(published, tmp_path):
    out = tmp_path / "site"
    export.export(published.config(), out=out, today=TODAY)
    text = all_text(out)
    for frag in sql_fragments(published.app_dir):
        assert frag not in text, frag
    for word in ("$argon2", "password_hash", "private_notes", "synthetic secret", "not for dashboards", "sqlite",
                 str(published.state_home), str(published.data_home), str(published.app_dir)):
        assert word not in text, word
    assert not re.search(r'"sql"\s*:', text)


def test_only_queries_used_by_components_are_exported(demo_app, tmp_path):
    f = demo_app.app_dir / "ui" / "dashboards" / "overview.yaml"
    f.write_text(f.read_text().replace(
        "queries:\n", "queries:\n  unused:\n    sql: SELECT count(*) AS hidden_count FROM activities\n", 1))
    out = tmp_path / "site"
    res = export.export(demo_app.config(), out=out, working=True, today=TODAY)
    p = payload_of(out)
    assert "unused" not in p["data"]["overview"]["results"] and "unused" not in p["specs"]["overview"]["queries"]
    assert "hidden_count" not in all_text(out)
    assert all(q["name"] != "unused" for q in res["visible"][0]["queries"])


def test_chosen_dashboards_and_values(published, tmp_path):
    out = tmp_path / "site"
    res = export.export(published.config(), out=out, dashboards=["activities"],
                        values=export.parse_values(["kind=run", "activities.min_km=5"]), today=TODAY)
    p = payload_of(out)
    assert list(p["specs"]) == ["activities"] and [d["slug"] for d in p["dashboards"]] == ["activities"]
    assert p["data"]["activities"]["values"]["kind"] == "run" and p["data"]["activities"]["values"]["min_km"] == 5
    assert {r[1] for r in p["data"]["activities"]["results"]["recent"]["rows"]} <= {"run"}
    assert "Overview" not in all_text(out)  # the other dashboard's title is not in the copy
    assert res["visible"][0]["values"]["kind"] == "run"


@pytest.mark.parametrize("vals, error", [
    (["nope=1"], UsageError),
    (["kind=jog"], Exception),
    (["min_km=lots"], Exception),
    (["bad value"], UsageError),
])
def test_bad_values_write_nothing(published, tmp_path, vals, error):
    out = tmp_path / "site"
    with pytest.raises(error):
        export.export(published.config(), out=out, values=export.parse_values(vals), today=TODAY)
    assert not out.exists()


def test_unknown_dashboard(published, tmp_path):
    with pytest.raises(NotFound):
        export.export(published.config(), out=tmp_path / "x", dashboards=["nope"], today=TODAY)


def test_encrypted_export_holds_no_readable_data(published, tmp_path):
    out = tmp_path / "site"
    res = export.export(published.config(), out=out, encrypt=True, today=TODAY)
    frag = res["fragment"]
    assert re.fullmatch(r"#k=[A-Za-z0-9_-]{43}", frag)
    text = all_text(out)
    key_text = frag[3:]
    assert key_text not in text
    for word in ("Overview", "Activities", "Demo project", "Resting heart rate", "distance_m", "run"):
        assert not re.search(rf"\b{re.escape(word)}\b", (out / "index.html").read_text()), word
    box = payload_of(out)
    assert box["enc"] == "A256GCM" and set(box) == {"v", "enc", "iv", "ct"}

    def unb64(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    plain = json.loads(AESGCM(unb64(key_text)).decrypt(unb64(box["iv"]), unb64(box["ct"]), export.AAD))
    assert plain["encrypted"] is True and set(plain["specs"]) == {"overview", "activities"}
    with pytest.raises(InvalidTag):
        AESGCM(unb64(key_text)).decrypt(unb64(box["iv"]), unb64(box["ct"]), b"other")
    # A new export has a new key and nonce.
    res2 = export.export(published.config(), out=out, encrypt=True, force=True, today=TODAY)
    assert res2["fragment"] != frag and payload_of(out)["iv"] != box["iv"]


def test_html_escaping_of_hostile_text(demo_app, tmp_path):
    f = demo_app.app_dir / "ui" / "dashboards" / "overview.yaml"
    f.write_text(f.read_text().replace("title: Overview", 'title: "</script><script>alert(1)</script>   <!--"', 1))
    out = tmp_path / "site"
    export.export(demo_app.config(), out=out, working=True, today=TODAY)
    html = (out / "index.html").read_text()
    assert html.count("</script>") == 2 and "<!--" not in html and " " not in html
    assert payload_of(out)["specs"]["overview"]["title"].startswith("</script><script>")


def test_page_policy_forbids_network(published, tmp_path):
    out = tmp_path / "site"
    export.export(published.config(), out=out, today=TODAY)
    html = (out / "index.html").read_text()
    assert "connect-src 'none'" in html and "default-src 'none'" in html
    assert "unsafe-inline" not in html and "<style" not in html
    assert not re.search(r"<script(?![^>]*(type=\"application/json\"|src=\"assets/))", html)
    js = (out / "assets" / "easyui-export.js").read_text()
    assert "fetch(" not in js and "XMLHttpRequest" not in js and "WebSocket" not in js
    assert "sendBeacon" not in js and "import(" not in js
    assert not re.search(r"^\s*(import|export)\s", js, re.M)


def test_bundle_is_valid_javascript(tmp_path):
    js = export.bundle_js()
    node = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()
    if not node:
        pytest.skip("node is not installed")
    f = tmp_path / "b.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bundle_refuses_what_it_does_not_understand(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text('import * as x from "./a.js";\n')
    with pytest.raises(EasyUIError) as e:
        export.bundle_js(tmp_path)
    assert getattr(e.value, "code", "") == "export_bundle"
    (assets / "app.js").write_text('const m = await import("./a.js");\n')
    with pytest.raises(EasyUIError):
        export.bundle_js(tmp_path)
    (assets / "app.js").write_text('import { a } from "./a.js";\n')
    (assets / "a.js").write_text('import { b } from "./app.js";\nexport const a = 1;\n')
    with pytest.raises(EasyUIError):
        export.bundle_js(tmp_path)


def test_out_folder_rules(published, tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "mine.txt").write_text("keep me")
    with pytest.raises(Refused):
        export.export(published.config(), out=out, today=TODAY)
    with pytest.raises(Refused):
        export.export(published.config(), out=out, force=True, today=TODAY)  # not an earlier export
    assert (out / "mine.txt").read_text() == "keep me"
    (out / ".easyui-export").write_text("planted")  # a marker alone does not make a folder replaceable
    with pytest.raises(Refused):
        export.export(published.config(), out=out, force=True, today=TODAY)
    assert (out / "mine.txt").read_text() == "keep me"
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(Refused):
        export.export(published.config(), out=link, today=TODAY)
    fresh = tmp_path / "fresh"
    export.export(published.config(), out=fresh, today=TODAY)
    export.export(published.config(), out=fresh, force=True, dashboards=["overview"], today=TODAY)
    assert list(payload_of(fresh)["specs"]) == ["overview"]
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".easyui-export")] == []


def test_query_error_stops_the_export(demo_app, tmp_path):
    f = demo_app.app_dir / "ui" / "dashboards" / "overview.yaml"
    f.write_text(f.read_text().replace("FROM days WHERE day >= :since ORDER BY day", "FROM days WHERE day >= :since ORDER BY nosuch", 1))
    out = tmp_path / "site"
    with pytest.raises(EasyUIError) as e:
        export.export(demo_app.config(), out=out, working=True, today=TODAY)
    assert e.value.code == "spec_invalid"
    assert not out.exists()


def test_query_failing_when_run_stops_the_export(published, tmp_path, monkeypatch):
    real = export.spec.run_queries

    def flaky(dash, names, values, **kw):
        out = real(dash, names, values, **kw)
        out["results"][names[-1]] = {"error": {"code": "query_timeout", "message": "it took too long", "hint": None}}
        return out

    monkeypatch.setattr(export.spec, "run_queries", flaky)
    out = tmp_path / "site"
    with pytest.raises(Refused) as e:
        export.export(published.config(), out=out, today=TODAY)
    assert e.value.code == "export_query_failed" and "it took too long" in e.value.message
    assert not out.exists()


def test_cli_export(published, tmp_path):
    out = tmp_path / "site"
    code, stdout, _ = run_cli("export", "--out", str(out), "--set", "period=week")
    assert code == 0
    assert "and nothing else" in stdout and "No SQL" in stdout and 'period="week"' in stdout
    code, data = run_cli_json("export", "--out", str(tmp_path / "locked"), "--encrypt")
    assert code == 0 and data["encrypted"] and data["fragment"].startswith("#k=")
    code, data = run_cli_json("export", "--out", str(out))
    assert code == 5 and data["error"]["code"] == "not_empty"
    code, data = run_cli_json("export", "--out", str(tmp_path / "x"), "--set", "nope=1")
    assert code == 2
    code, data = run_cli_json("export")
    assert code == 2


def test_first_dashboard_of_the_project_is_the_landing_page(demo_app, tmp_path):
    """The page opens on the first dashboard of the list; the list follows [ui] dashboards, also in a copy."""
    toml = demo_app.app_dir / "project.toml"
    toml.write_text(toml.read_text().replace('dashboards = ["overview", "activities"]', 'dashboards = ["activities", "overview"]'))
    out = tmp_path / "site"
    export.export(demo_app.config(), out=out, working=True, today=TODAY)
    assert [d["slug"] for d in payload_of(out)["dashboards"]] == ["activities", "overview"]
    app_js = (Path(export.WEB_DIR) / "assets" / "app.js").read_text()
    assert "window.location.replace(`#/d/${state.dashboards[0].slug}`)" in app_js
