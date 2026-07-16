"""
Tests for the human-in-the-loop (HITL) missing-file request feature.

When the agent loop issues a <file_request> for a file that is neither in the
session nor auto-fetchable from GitHub, it now PAUSES and asks the user to
supply the file over the WebSocket back-channel (asyncio.Queue "inbox"),
instead of silently giving up and wasting the run.

Two layers are covered:

  1. FUNCTIONAL — the *actual* nested async generator `_await_user_file` is
     extracted from backend/services/pipeline.py and executed in isolation
     with light stubs. This proves the real shipped code handles all four
     outcomes: file provided, user skipped, timeout, and no back-channel.

  2. SOURCE — structural assertions that the pipeline agent-loop funnels both
     file-not-found dead-ends into the pause, and that chat.py wires the inbox
     through the WS transport (shim + concurrent receiver) while leaving the
     single-pass / offline signatures untouched.
"""
import ast
import asyncio
import os
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))
_CHAT = os.path.normpath(os.path.join(_HERE, "..", "backend", "routers", "chat.py"))


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _extract_nested_async(src, name):
    """Return the source text of a nested async function by name (ast.walk)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"async function {name} not found in {path}")


def _make_await_user_file(client_inbox, *, wait_timeout=1.0, over_budget=False):
    """Exec the real _await_user_file with stubbed closure globals and return
    a bound callable. All free variables it references are supplied here."""
    src = _read(_PIPELINE)
    fn_src = _extract_nested_async(src, "_await_user_file")
    events = []

    ns = {
        "asyncio": asyncio,
        "time": time,
        "sse": lambda obj: obj,               # keep the dict so tests can inspect it
        "_dlog": lambda *a, **k: events.append((a, k)),
        "session_id": "sess-1",
        "user_id": "user-1",
        "client_inbox": client_inbox,
        "FILE_WAIT_TIMEOUT_S": wait_timeout,
        "_pipeline_over_budget": (lambda: over_budget),
    }
    exec(compile(fn_src, _PIPELINE, "exec"), ns)
    return ns["_await_user_file"], events


async def _drive(await_user_file, missing_fn, outcome, feed=None, feed_after=0.0):
    """Run the async generator, optionally putting a client reply on the inbox
    after `feed_after` seconds. Returns the list of yielded SSE dicts."""
    yielded = []

    async def _feeder(inbox):
        await asyncio.sleep(feed_after)
        await inbox.put(feed)

    task = None
    async for ev in await_user_file(missing_fn, outcome):
        yielded.append(ev)
        # Kick off the feeder as soon as the file_needed prompt is emitted.
        if feed is not None and task is None and ev.get("type") == "file_needed":
            # inbox is captured via closure global; fetch it back out.
            task = asyncio.create_task(_feeder(_current_inbox))
    if task:
        await task
    return yielded


# _current_inbox is set by each test right before driving, so the feeder can
# reach the same queue the generator awaits on.
_current_inbox = None


class AwaitUserFileFunctional(unittest.IsolatedAsyncioTestCase):

    async def test_file_provided_resumes(self):
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox)
        outcome = {}
        reply = {"type": "file_response", "filename": "database.py",
                 "content": "def connect(): return 'db'\n"}
        yielded = await _drive(fn, "database.py", outcome, feed=reply, feed_after=0.05)
        self.assertEqual(outcome.get("content"), reply["content"])
        self.assertEqual(outcome.get("filename"), "database.py")
        self.assertNotIn("message", outcome)
        types_ = [e.get("type") for e in yielded]
        self.assertEqual(types_[0], "file_needed")
        self.assertEqual(types_[-1], "file_needed_cleared")

    async def test_user_skip(self):
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox)
        outcome = {}
        reply = {"type": "file_response", "action": "skip"}
        yielded = await _drive(fn, "database.py", outcome, feed=reply, feed_after=0.05)
        self.assertNotIn("content", outcome)
        self.assertIn("SKIPPED", outcome.get("message", ""))
        self.assertEqual(yielded[-1].get("type"), "file_needed_cleared")

    async def test_reply_without_content_treated_as_skip(self):
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox)
        outcome = {}
        reply = {"type": "file_response", "filename": "x.py"}   # no content
        yielded = await _drive(fn, "database.py", outcome, feed=reply, feed_after=0.05)
        self.assertNotIn("content", outcome)
        self.assertIn("SKIPPED", outcome.get("message", ""))

    async def test_timeout_no_reply(self):
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox, wait_timeout=0.3)
        outcome = {}
        yielded = await _drive(fn, "database.py", outcome, feed=None)
        self.assertNotIn("content", outcome)
        self.assertIn("NOT PROVIDED", outcome.get("message", ""))
        self.assertEqual(yielded[-1].get("type"), "file_needed_cleared")

    async def test_over_budget_bails_fast(self):
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox, wait_timeout=999, over_budget=True)
        outcome = {}
        t0 = time.time()
        yielded = await _drive(fn, "database.py", outcome, feed=None)
        self.assertLess(time.time() - t0, 2.0, "over-budget must not wait the full timeout")
        self.assertIn("NOT PROVIDED", outcome.get("message", ""))

    async def test_no_backchannel_degrades(self):
        fn, _ev = _make_await_user_file(None)   # HTTP/SSE / offline transport
        outcome = {}
        yielded = [ev async for ev in fn("database.py", outcome)]
        self.assertEqual(yielded, [], "no back-channel must not emit any prompt")
        self.assertNotIn("content", outcome)
        self.assertIn("NOT FOUND", outcome.get("message", ""))
        self.assertIn("stop", outcome.get("message", "").lower())

    async def test_stale_reply_is_drained(self):
        """A leftover reply from a previous request must not be consumed as the
        answer to a new one."""
        global _current_inbox
        _current_inbox = asyncio.Queue()
        # Pre-load a stale reply BEFORE the prompt is issued.
        await _current_inbox.put({"type": "file_response", "content": "STALE"})
        fn, _ev = _make_await_user_file(_current_inbox, wait_timeout=0.3)
        outcome = {}
        # Feed nothing new → should time out, NOT pick up the stale content.
        yielded = await _drive(fn, "database.py", outcome, feed=None)
        self.assertNotEqual(outcome.get("content"), "STALE")
        self.assertIn("NOT PROVIDED", outcome.get("message", ""))

    async def test_retry_prompt_flags_and_wording(self):
        """A same-name re-request (is_retry=True) emits retry:true and asks for
        the *correct* file, so the user knows the last upload was wrong."""
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox, wait_timeout=0.3)
        outcome = {}
        yielded = []
        async for ev in fn("database.py", outcome, is_retry=True):
            yielded.append(ev)
        prompt = next(e for e in yielded if e.get("type") == "file_needed")
        self.assertTrue(prompt.get("retry"))
        self.assertIn("right", prompt.get("content", "").lower())

    async def test_first_request_prompt_is_not_retry(self):
        """A first request emits retry:false (default) with the standard ask."""
        global _current_inbox
        _current_inbox = asyncio.Queue()
        fn, _ev = _make_await_user_file(_current_inbox, wait_timeout=0.3)
        outcome = {}
        yielded = []
        async for ev in fn("database.py", outcome):
            yielded.append(ev)
        prompt = next(e for e in yielded if e.get("type") == "file_needed")
        self.assertFalse(prompt.get("retry"))


class PipelineSourceStructure(unittest.TestCase):

    def setUp(self):
        self.src = _read(_PIPELINE)

    def test_signature_has_client_inbox(self):
        self.assertIn("client_inbox=None", self.src)

    def test_helper_defined(self):
        self.assertIn(
            "async def _await_user_file(missing_fn: str, outcome: dict, is_retry: bool = False):",
            self.src)

    def test_both_deadends_funnel_to_pause(self):
        # The unified second check delegates to the pause helper, threading the
        # retry flag so a same-name correction gets the right prompt.
        self.assertIn("_await_user_file(", self.src)
        self.assertIn("is_retry=_is_reretry", self.src)
        self.assertIn("yield _fr_ev", self.src)

    def test_provided_file_is_registered(self):
        # On a provided file the caller injects it into the stream lookup +
        # symbol map so the model finds it immediately.
        self.assertIn('content = _fr_outcome["content"]', self.src)
        self.assertIn("file_content_lookup_stream[fn] = content", self.src)

    def test_dlog_on_all_outcomes(self):
        for tag in (
            "agent_filereq_pause_ask",
            "agent_filereq_pause_provided",
            "agent_filereq_pause_skip",
            "agent_filereq_pause_timeout",
            "agent_filereq_no_backchannel",
        ):
            self.assertIn(tag, self.src, f"missing _dlog tag: {tag}")

    def test_emits_file_events(self):
        self.assertIn('"type": "file_needed"', self.src)
        self.assertIn('"type": "file_needed_cleared"', self.src)

    def test_same_name_retry_state_declared(self):
        # The retry budget + tracking sets/dicts must exist.
        self.assertIn("MAX_SAME_FILE_RETRIES", self.src)
        self.assertIn("user_supplied_files", self.src)
        self.assertIn("same_name_retries", self.src)

    def test_rerequestable_gated_to_user_supplied(self):
        # A re-request is only allowed for user-supplied files under budget.
        self.assertIn("_rerequestable", self.src)
        self.assertIn("fn in user_supplied_files", self.src)
        self.assertIn("same_name_retries.get(fn, 0) < MAX_SAME_FILE_RETRIES", self.src)

    def test_reretry_forces_fresh_pause_and_skips_github(self):
        # On a re-retry the wrong copy is dropped and GitHub is skipped.
        self.assertIn("_is_reretry = fn in _rerequestable", self.src)
        self.assertIn("if not _is_reretry and _gh_nat_enabled and _gh_known_repos:", self.src)
        self.assertIn("is_retry=_is_reretry", self.src)

    def test_reretry_consumes_budget(self):
        # Each honored correction spends one unit so it can't loop forever.
        self.assertIn("same_name_retries[fn] = same_name_retries.get(fn, 0) + 1", self.src)

    def test_provided_file_marked_user_supplied(self):
        # A user-provided file is tagged so it becomes re-requestable if wrong.
        self.assertIn("user_supplied_files.add(fn)", self.src)

    def test_total_limit_only_blocks_new_distinct_files(self):
        # The 15-file cap must not block re-requesting an already-loaded file.
        self.assertIn("_new_distinct", self.src)
        self.assertIn("_hit_total_limit", self.src)

    def test_single_pass_signature_untouched(self):
        # run_smart_pipeline_stream must NOT have gained client_inbox.
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_smart_pipeline_stream":
                arg_names = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
                self.assertNotIn("client_inbox", arg_names,
                                 "single-pass pipeline signature must stay untouched")
                return
        self.fail("run_smart_pipeline_stream not found")


class ChatWiringSourceStructure(unittest.TestCase):

    def setUp(self):
        self.src = _read(_CHAT)

    def test_shim_carries_inbox(self):
        self.assertIn("client_inbox=None", self.src)
        self.assertIn("client_inbox=client_inbox", self.src)

    def test_ws_pump_creates_inbox_and_receiver(self):
        self.assertIn("inbox: asyncio.Queue = asyncio.Queue()", self.src)
        self.assertIn("async def _client_receiver():", self.src)
        self.assertIn('msg_in.get("type") == "file_response"', self.src)
        self.assertIn("recv_task = asyncio.create_task(_client_receiver())", self.src)

    def test_receiver_cancelled_in_finally(self):
        self.assertIn("recv_task.cancel()", self.src)

    def test_smart_stream_threads_inbox_only_for_natural(self):
        self.assertIn("if _pipeline is run_natural_pipeline_stream:", self.src)
        self.assertIn('_pipe_kwargs["client_inbox"] = getattr(', self.src)

    def test_execute_task_threads_inbox(self):
        self.assertIn('client_inbox=getattr(request.state, "client_inbox", None)', self.src)


if __name__ == "__main__":
    unittest.main()
