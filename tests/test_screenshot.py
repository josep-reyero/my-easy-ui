"""easy-ui screenshot: finding a browser, the plain message without one, and (when one is installed) a real picture."""

from __future__ import annotations

import os
import struct

import pytest
from conftest import run_cli_json

from easyui import screenshot


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


@pytest.mark.parametrize("args", [["--out", "a.jpg"], ["--out", "a.png", "--width", "10"], ["--out", "a.png", "--height", "5"]])
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
