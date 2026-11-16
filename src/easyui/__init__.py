"""my-easy-ui: a dashboard server and browser renderer for one app at a time.

A dashboard is a disposable, declarative view (a YAML file) over a read-only
copy of an app's data. People and their AI agents can reshape dashboards at
any time; the format never contains JavaScript, HTML or CSS.

Modules
-------
``easyui.errors``     error types, exit codes and HTTP statuses
``easyui.paths``      per-machine folders (app data, UI state)
``easyui.appconfig``  reading ``app.toml`` ([app] and [ui])
``easyui.formats``    the value-format vocabulary (shared with the renderer)
``easyui.spec``       loading, validating and compiling dashboard files
``easyui.sandbox``    the read-only query sandbox
``easyui.bundle``     building the dashboard data copy, the current link, rev
``easyui.cli``        the ``easy-ui`` command line

Public names: ``__version__``, ``SPEC_VERSION``.
"""

__version__ = "0.1.0"
SPEC_VERSION = "easy-ui/v1"
