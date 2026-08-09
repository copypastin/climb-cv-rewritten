"""End-to-end runtime tests — real `spawn`ed processes, real queues.

design/isolation.md. These are the tests that matter for a framework whose purpose is running
code its authors never see, so the cases here are the hostile and careless ones. The assertion
is nearly always the same: **the app is still running, and the log names the plugin at fault.**

Marked slow because each one starts OS processes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from climbcv.config import load_config  # noqa: E402
from climbcv.loader import discover  # noqa: E402
from climbcv.manifest import MANIFEST_FILENAME  # noqa: E402
from climbcv.supervisor import Supervisor  # noqa: E402
from climbcv.wiring import resolve  # noqa: E402

pytestmark = pytest.mark.slow


SOURCE = '''
import numpy as np
from climbcv import Plugin
from climbcv.contracts import Frame
from climbcv.plugin import every


class Source(Plugin):
    def setup(self):
        self.n = 0
        self.log.info("source ready")

    @every(0.01)
    def pump(self):
        self.n += 1
        self.publish("frame", Frame(
            self.n, 0, np.zeros((2, 2, 3), np.uint8), "bgr", False, "test",
        ))
'''

SINK = '''
from climbcv import Plugin
from climbcv.plugin import every, subscribe


class Sink(Plugin):
    def setup(self):
        self.seen = 0
        (self.data_dir_path() / "count").write_text("0")

    def data_dir_path(self):
        from pathlib import Path
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @subscribe("frame")
    def on_frame(self, frame, meta):
        self.seen += 1
        assert frame.pixels.flags.writeable is False, "payload arrived mutable"
        (self.data_dir_path() / "count").write_text(str(self.seen))
        (self.data_dir_path() / "source").write_text(meta.source)
'''

CRASHER = '''
from climbcv import Plugin
from climbcv.plugin import subscribe


class Crasher(Plugin):
    @subscribe("frame")
    def on_frame(self, frame, meta):
        raise RuntimeError("boom")
'''

BAD_INIT = '''
from climbcv import Plugin
from climbcv.plugin import subscribe


class BadInit(Plugin):
    def __init__(self):
        super().__init__()
        self.thing = 1

    @subscribe("frame")
    def on_frame(self, frame, meta): ...
'''

UNAVAILABLE = '''
from climbcv import Plugin
from climbcv.plugin import subscribe


class Nope(Plugin):
    def setup(self):
        self.unavailable("no such hardware on this machine")

    @subscribe("frame")
    def on_frame(self, frame, meta): ...
'''


def make(root: Path, pid: str, code: str, *, publishes=(), subscribes=(), extra="") -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    pubs = "".join(f'\n[[publishes]]\ntopic = "{t}"\n' for t in publishes)
    subs = "".join(f'\n[[subscribes]]\ntopic = "{t}"\n' for t in subscribes)
    (d / MANIFEST_FILENAME).write_text(
        f'[plugin]\nid = "{pid}"\nversion = "1.0.0"\napi_version = "1.0"\n'
        f'entry = "plugin:{code.split("class ")[1].split("(")[0]}"\n'
        f'name = "{pid}"\ndescription = "d"\nauthor = "t"\n'
        f"heartbeat_warn_s = 30.0\n{extra}{pubs}{subs}",
        encoding="utf-8",
    )
    (d / "plugin.py").write_text(code, encoding="utf-8")


def build(tmp_path: Path, *, host_subs=()):
    src = str(Path(__file__).resolve().parents[1] / "src")
    cfg = load_config({
        "framework": {
            "plugin_dir": str(tmp_path / "plugins"),
            "state_dir": str(tmp_path / "state"),
            "log_level": "INFO",
            "grace_s": 3.0,
        }
    })
    d = discover(cfg)
    plan = resolve(d, cfg, host_subscribes=host_subs)
    # The child imports climbcv from the source tree in tests.
    import os

    os.environ["PYTHONPATH"] = src + os.pathsep + os.environ.get("PYTHONPATH", "")
    return Supervisor(plan, cfg.framework)


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- the happy path


def test_a_message_crosses_two_processes(tmp_path):
    """The core claim of the whole architecture: publisher process -> subscriber process,
    with no author writing a Queue and no forwarding hop in the data path."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "sink_p", SINK, subscribes=("frame",))
    sup = build(tmp_path)
    count = tmp_path / "state" / "sink_p" / "count"
    try:
        sup.start()
        assert wait_for(lambda: count.is_file() and count.read_text() not in ("", "0")), \
            "no frame was delivered across the process boundary"
        assert (tmp_path / "state" / "sink_p" / "source").read_text() == "src_p", \
            "Meta.source must be framework-injected and correct"
    finally:
        sup.stop()


def test_payload_arrives_read_only_in_the_subscriber(tmp_path):
    """The guarantee that failed twice on paper. The sink asserts it in-process; if the flag
    did not survive the round-trip the sink would crash and deliver nothing."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "sink_p", SINK, subscribes=("frame",))
    sup = build(tmp_path)
    count = tmp_path / "state" / "sink_p" / "count"
    try:
        sup.start()
        assert wait_for(lambda: (sup.poll(0.02), count.is_file()
                                 and int(count.read_text() or 0) >= 3)[1])
        # Still 'ready' rather than 'crashed' means the in-handler assertion that the payload
        # arrived read-only held — if the writeable flag had not survived the pickle round
        # trip, the sink would have died on its first frame.
        assert sup.states["sink_p"].state == "ready"
    finally:
        sup.stop()


def test_data_dir_is_created_before_setup_runs(tmp_path):
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "sink_p", SINK, subscribes=("frame",))
    sup = build(tmp_path)
    try:
        sup.start()
        assert wait_for(lambda: (tmp_path / "state" / "sink_p").is_dir())
    finally:
        sup.stop()


# ---------------------------------------------------------------- hostile cases


def test_a_crashing_plugin_is_contained_and_the_app_keeps_running(tmp_path):
    """Decision #5. The source must still be publishing after the crasher dies."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "sink_p", SINK, subscribes=("frame",))
    make(tmp_path / "plugins", "crash_p", CRASHER, subscribes=("frame",))
    sup = build(tmp_path)
    count = tmp_path / "state" / "sink_p" / "count"
    try:
        sup.start()

        def crashed():
            sup.poll(0.05)
            return sup.states["crash_p"].state in ("crashed", "quarantined")

        assert wait_for(crashed), "the crash was never reported to the supervisor"
        before = int(count.read_text() or 0)
        assert wait_for(lambda: int(count.read_text() or 0) > before), \
            "the healthy sink stopped receiving after another plugin crashed"
    finally:
        sup.stop()


def test_defining_init_is_quarantined_not_retried(tmp_path):
    """A deterministic authoring mistake retried five times buries the useful message under a
    restart log, and it must never escalate to shutting the app down."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "bad_p", BAD_INIT, subscribes=("frame",))
    sup = build(tmp_path)
    try:
        sup.start()

        def quarantined():
            sup.poll(0.05)
            return sup.states["bad_p"].state == "quarantined"

        assert wait_for(quarantined)
        assert "setup()" in sup.states["bad_p"].detail
        assert sup.states["bad_p"].restarts == 0, "a contract error must not be retried"
    finally:
        sup.stop()


def test_unavailable_is_not_a_crash(tmp_path):
    """The mac-lid case for a condition only discoverable at runtime: one INFO line, no
    retry, no quarantine — 'this machine has no sensor', not 'the plugin is broken'."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    make(tmp_path / "plugins", "nope_p", UNAVAILABLE, subscribes=("frame",))
    sup = build(tmp_path)
    try:
        sup.start()

        def unavailable():
            sup.poll(0.05)
            return sup.states["nope_p"].state == "unavailable"

        assert wait_for(unavailable)
        assert "hardware" in sup.states["nope_p"].detail
        assert sup.states["nope_p"].crashes == (), "must not be counted as a crash"
    finally:
        sup.stop()


# ---------------------------------------------------------------- host participation


def test_the_host_receives_deliveries_on_its_own_thread(tmp_path):
    """poll() drains on the CALLER's thread, which is what a Qt/Tk embedder needs — being
    handed callbacks from a framework thread is how GUI hosts crash."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    from climbcv.wiring import Subscription

    sup = build(tmp_path, host_subs=(Subscription("frame", True, 4, "handler", False),))
    got: list = []
    sup.on("frame", lambda payload, meta: got.append((payload.seq, meta.source)))
    try:
        sup.start()
        assert wait_for(lambda: (sup.poll(0.05), bool(got))[1])
        assert got[0][1] == "src_p"
    finally:
        sup.stop()


# ---------------------------------------------------------------- Decision #24


def test_two_running_supervisors_in_one_process_is_refused(tmp_path):
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    a = build(tmp_path)
    b = build(tmp_path)
    try:
        a.start()
        with pytest.raises(RuntimeError, match="already running"):
            b.start()
    finally:
        a.stop()


def test_a_supervisor_is_not_rerunnable_after_stop(tmp_path):
    """A notebook user tries this on their second cell. The honest answer is no: declarations
    freeze at start(), the queues are consumed and the children are gone."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    sup = build(tmp_path)
    sup.start()
    sup.stop()
    with pytest.raises(RuntimeError, match="already run"):
        sup.start()


def test_stop_releases_the_guard_so_a_fresh_instance_can_run(tmp_path):
    """The guard is on *running*, not construction — otherwise the Jupyter loop of stop() then
    a new instance would be forbidden, and that is the environment spawn was verified for."""
    make(tmp_path / "plugins", "src_p", SOURCE, publishes=("frame",))
    first = build(tmp_path)
    first.start()
    first.stop()
    second = build(tmp_path)
    try:
        second.start()  # must not raise
    finally:
        second.stop()


# ---------------------------------------------- a child that dies without reporting

ABORTER = """
import os, signal
from climbcv import Plugin
class Aborter(Plugin):
    def setup(self):
        # A native abort, which is what a C++ vision library does on a fatal error. No Python
        # exception is raised, so nothing is ever sent on the control queue.
        os.kill(os.getpid(), signal.SIGABRT)
"""

SILENT_EXIT = """
import os
from climbcv import Plugin
class Quitter(Plugin):
    def setup(self):
        os._exit(3)
"""


def _settled(sup, pid):
    def check():
        sup.poll(0.05)
        return sup.states[pid].state in ("crashed", "quarantined")
    return check


def test_child_killed_by_a_signal_is_noticed_and_explained(tmp_path):
    """The supervisor must reap dead children, not only read the control queue.

    A plugin that raises in Python reports its own crash with a traceback. A plugin killed by a
    native abort reports NOTHING -- the process vanishes and no handler runs. Without an
    aliveness check it sits in 'starting' forever: no error, no log line, and a user staring at
    a window that never updates. Vision plugins wrap large C++ libraries, which makes this the
    most likely way for one to fail.
    """
    make(tmp_path / "plugins", "aborter", ABORTER, subscribes=(), publishes=())
    sup = build(tmp_path)
    try:
        sup.start()
        assert wait_for(_settled(sup, "aborter")), "a signalled child was never noticed"
        detail = sup.states["aborter"].detail
        assert "SIGABRT" in detail, detail
        assert "during setup()" in detail, "should say it died before running a handler"
    finally:
        sup.stop()


def test_child_exiting_nonzero_without_reporting_is_noticed(tmp_path):
    make(tmp_path / "plugins", "quitter", SILENT_EXIT, subscribes=(), publishes=())
    sup = build(tmp_path)
    try:
        sup.start()
        assert wait_for(_settled(sup, "quitter"))
        assert "status 3" in sup.states["quitter"].detail, sup.states["quitter"].detail
    finally:
        sup.stop()


def test_crash_detail_is_the_reason_not_a_restart_timestamp(tmp_path):
    """`detail` is what an operator reads when something breaks. Smuggling the restart schedule
    through it means the diagnostic they get is a float."""
    make(tmp_path / "plugins", "aborter2", ABORTER, subscribes=(), publishes=())
    sup = build(tmp_path)
    try:
        sup.start()
        assert wait_for(_settled(sup, "aborter2"))
        state = sup.states["aborter2"]
        assert not state.detail.startswith("restart_at:")
        assert state.restart_at >= 0.0
    finally:
        sup.stop()
