"""easy-ui screenshot: finding a browser, the plain message without one, and (when one is installed) a real picture."""

from __future__ import annotations

import os
import struct

import pytest
from conftest import run_cli_json

from easyui import screenshot
from easyui.errors import UsageError


def test_chosen_browser(tmp_path):
    exe = tmp_path / "chromium"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    assert screenshot.find_browser({"EASYUI_CHROMIUM": str(exe)}) == str(exe)
    assert screenshot.find_browser({"EASYUI_CHROMIUM": str(tmp_path / "missing")}) is None
    os.chmod(exe, 0o644)
    assert screenshot.find_browser({"EASYUI_CHROMIUM": str(exe)}) is None


def test_no_browser_is_a_plain_message(demo_app, monkeypatch, tmp_path):
    monkeypatch.setenv("EASYUI_CHROMIUM", str(tmp_path / "missing"))
    code, data = run_cli_json("screenshot", "--out", str(tmp_path / "a.png"))
    assert code == 3 and data["error"]["code"] == "browser_missing"
    assert "EASYUI_CHROMIUM" in data["error"]["hint"]


@pytest.mark.parametrize("args", [["--out", "a.jpg"], ["--out", "a.png", "--width", "10"], ["--out", "a.png", "--height", "5"],
                                  ["--out", "a.png", "--scale", "4"], ["--out", "a.png", "--scale", "0"],
                                  ["--out", "a.png", "--width", "3840", "--scale", "3"]])
def test_bad_options(demo_app, tmp_path, args):
    code, data = run_cli_json("screenshot", *args)
    assert code == 2


def png_size(path):
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


@pytest.mark.skipif(not screenshot.find_browser(), reason="no Chromium-based browser installed")
def test_real_picture(demo_app, tmp_path):
    out = tmp_path / "shot.png"
    code, data = run_cli_json("screenshot", "overview", "--out", str(out), "--phone", "--dark")
    assert code == 0, data
    assert data["dashboard"] == "overview" and data["dark"] and data["phone"]
    w, h = png_size(out)
    assert w == 2 * 390 and h > 2 * 844  # the whole page, at phone pixel density
    assert data["page_title"].startswith("Overview")
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_scale_is_checked_before_a_browser_is_looked_for(demo_app, tmp_path):
    with pytest.raises(UsageError) as e:
        screenshot.screenshot(demo_app.config(), out=tmp_path / "a.png", scale=4)
    assert "1, 2 or 3" in e.value.message
    with pytest.raises(UsageError):
        screenshot.screenshot(demo_app.config(), out=tmp_path / "a.png", width=3000, scale=3)


@pytest.mark.skipif(not screenshot.find_browser(), reason="no Chromium-based browser installed")
def test_desktop_pictures_are_sharp_by_default(demo_app, tmp_path):
    """Desktop pictures are taken at 2 device pixels per CSS pixel too (they were soft on high-density screens)."""
    out = tmp_path / "desk.png"
    code, data = run_cli_json("screenshot", "overview", "--out", str(out), "--height", "600")
    assert code == 0, data
    assert data["scale"] == 2 and png_size(out) == (2 * 1280, 2 * 600) and data["pixels"] == [2560, 1200]
    code, data = run_cli_json("screenshot", "overview", "--out", str(out), "--height", "600", "--scale", "1")
    assert code == 0 and png_size(out) == (1280, 600)


def _page_facts(html, *, width, scale, fragment, expression):
    """Open an exported page in the headless browser at a width and pixel density, and evaluate an expression."""
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile
    import time as _time

    browser = screenshot.find_browser()
    profile = _tempfile.mkdtemp(prefix="easyui-test-browser-")
    args = ["--remote-debugging-pipe", f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--disable-extensions", "--hide-scrollbars", "about:blank"]
    if "headless-shell" not in os.path.basename(browser):
        args.insert(0, "--headless=new")
    pipe = screenshot.Pipe(browser, args, timeout=60)
    try:
        target = pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        pipe.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": scale,
                                                          "mobile": False}, session)
        pipe.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__v=[];document.addEventListener("
                  "'securitypolicyviolation',e=>window.__v.push(e.violatedDirective),true);"}, session)
        pipe.call("Page.enable", {}, session)
        pipe.call("Page.navigate", {"url": html.resolve().as_uri() + fragment}, session)
        while not pipe.call("Runtime.evaluate", {"expression": screenshot._READY, "returnByValue": True},
                            session).get("result", {}).get("value"):
            _time.sleep(0.15)
        _time.sleep(0.6)
        res = pipe.call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session)
        return _json.loads(res["result"]["value"])
    finally:
        pipe.close()
        _shutil.rmtree(profile, ignore_errors=True)


FACTS = """JSON.stringify((() => {
  const root = document.querySelector('.eui-host').shadowRoot;
  const charts = [...root.querySelectorAll('.eui-chart')];
  return {
    charts: charts.length,
    svg: charts.filter((c) => c.querySelector('svg')).length,
    canvas: charts.reduce((n, c) => n + c.querySelectorAll('canvas').length, 0),
    sized: charts.every((c) => { const s = c.querySelector('svg'); return s && Math.abs(+s.getAttribute('width') - c.clientWidth) <= 1; }),
    cols: [...root.querySelectorAll('.rows > .row')].map((r) => r.dataset.cols),
    sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    violations: window.__v,
  };
})())"""


@pytest.mark.skipif(not screenshot.find_browser(), reason="no Chromium-based browser installed")
@pytest.mark.parametrize(("width", "scale"), [(1280, 1), (390, 3)])
def test_charts_are_svg_within_the_policy_at_any_width(demo_app, tmp_path, width, scale):
    """The exported demo: every chart is SVG at its box's size, nothing breaks the page's policy, and the rows
    share the width: three numbers across on a desktop, two on a phone, and no sideways scrolling."""
    from easyui import export

    export.export(demo_app.config(), out=tmp_path / "site", working=True, picture=True)
    facts = _page_facts(tmp_path / "site" / "index.html", width=width, scale=scale, fragment="#/d/overview",
                        expression=FACTS)
    assert facts["charts"] >= 4 and facts["svg"] == facts["charts"] and facts["canvas"] == 0 and facts["sized"]
    assert facts["violations"] == [] and facts["sideways"] is False
    stats = facts["cols"][1]
    assert stats == ("3" if width >= 1000 else "2")
