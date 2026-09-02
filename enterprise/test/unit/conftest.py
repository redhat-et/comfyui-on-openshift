"""
Shared fixtures for the unit test layer (T1, docs' AUDIT-AND-PLAN.md).

This suite imports enterprise/gateway/hub.py and enterprise/worker/
worker_agent.py directly, as `hub` and `worker_agent`, and calls their pure
functions in-process -- no Redis, no ComfyUI, no subprocess. That only works
if importing either file is side-effect free, so both are checked below
rather than assumed.

hub.py at import time: reads its whole configuration block from
os.environ.get() with defaults and raises ValueError only if EVENT_STREAM_TTL
or REAPER_INTERVAL come out <= 0 (they don't, at their defaults, so nothing
here sets them). It builds a FastAPI `app` object and resolves OUTPUT_ROOT as
a Path, neither of which touches Redis or the filesystem in a way that can
fail -- `client()` creates the redis.asyncio.Redis object lazily, on its own
first call, which no test in this suite makes. So hub imports cleanly with no
environment set at all.

worker_agent.py at import time DOES have one real side effect: BEGIN WORKER
IDENTITY installs real signal handlers --
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
(worker_agent.py:862-863) so a running agent drains the job it is on instead
of being killed mid-generation. Importing the module in the test process
overwrites pytest's own SIGINT handler with handle_sigterm, which would turn
Ctrl-C during a test run into "SIGTERM received -- finishing the current job,
then exiting" instead of a KeyboardInterrupt. That is harmless for a batch
CI run but actively unfriendly to a contributor running this locally, so the
import is guarded: capture whatever handlers were installed before the
import, import the module, then put them back.
"""

from __future__ import annotations

import pathlib
import signal
import sys

import pytest

_GATEWAY_DIR = pathlib.Path(__file__).resolve().parents[2] / "gateway"
_WORKER_DIR = pathlib.Path(__file__).resolve().parents[2] / "worker"

for _path in (str(_GATEWAY_DIR), str(_WORKER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import hub  # noqa: E402  (path must be extended first)

_prior_sigterm = signal.getsignal(signal.SIGTERM)
_prior_sigint = signal.getsignal(signal.SIGINT)

import worker_agent  # noqa: E402

# Restore whatever pytest (or the shell) had installed. Only meaningful when
# running on the main thread with real signal support, which is what every
# supported test runner does; guarded anyway since signal.signal outside the
# main thread raises ValueError instead of silently no-op-ing.
try:
    signal.signal(signal.SIGTERM, _prior_sigterm)
    signal.signal(signal.SIGINT, _prior_sigint)
except ValueError:
    pass


@pytest.fixture
def hub_module():
    """The imported hub module, as a fixture so tests can be parametrized
    over `hub` and `worker_agent` interchangeably (see test_envelope.py and
    test_workspace_name.py, which run the same assertions against each
    file's copy of a MIRRORED VERBATIM block)."""
    return hub


@pytest.fixture
def worker_agent_module():
    return worker_agent


@pytest.fixture
def hub_output_root(tmp_path, monkeypatch):
    """Point hub.OUTPUT_ROOT at a throwaway directory for one test, for
    locate_output() -- same reasoning as worker_output_root below."""
    monkeypatch.setattr(hub, "OUTPUT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def worker_output_root(tmp_path, monkeypatch):
    """
    Point worker_agent.OUTPUT_ROOT at a throwaway directory for one test.

    The functions that touch the filesystem (workspace_path, ensure_workspace,
    output_subfolder) all read the module-level OUTPUT_ROOT global on every
    call rather than taking it as a parameter, so monkeypatching the module
    attribute -- not the environment variable, which is only read once, at
    import time -- is what actually redirects them.
    """
    monkeypatch.setattr(worker_agent, "OUTPUT_ROOT", tmp_path)
    return tmp_path
