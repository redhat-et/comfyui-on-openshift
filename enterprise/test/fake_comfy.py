"""Stub ComfyUI: just enough API surface to exercise worker_agent.py."""
import asyncio, json, uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect


# ---------------------------------------------------------------------------
# THE OPENING HANDSHAKE HAS TO BE ALLOWED TO TAKE LONGER THAN TEN SECONDS.
#
# stall_next_ws_s below parks a connection BEFORE ws.accept(), and under ASGI
# the accept IS the handshake response: uvicorn runs this module's websocket
# endpoint from inside websockets' opening handshake, and the legacy
# implementation (websockets.legacy.server.WebSocketServerProtocol, which
# uvicorn's WebSocketProtocol subclasses) wraps that whole step in
# `open_timeout` — ten seconds, defaulted in websockets and passed by no
# uvicorn setting.
#
# Past that ceiling the stall stops being a stall. The handshake is abandoned,
# the TCP connection is dropped, worker_agent.py's ws.connect() raises
# WebSocketConnectionClosedException("Connection to remote host was lost."),
# and a fixture that promised "ComfyUI is SLOW to accept" has delivered
# "ComfyUI is DEAD" instead. That is the one substitution it must never make:
# a check about what a live worker does while parked is then asserting it
# against a job that never ran at all. check-36-live-worker-fencing.py stalls
# for HEARTBEAT_TTL + 5 — 15s with the TTL run.sh exports — so it sat the
# wrong side of that ceiling and read the dead ComfyUI as its own subject.
#
# Only the legacy implementation carries this timer: uvicorn's sansio and
# wsproto websocket implementations time the opening handshake nowhere, so
# for those there is nothing to lift and this is a no-op. That reasoning is
# not trusted to stay true — check-36 times the park it gets and asserts it
# lasted, so a ceiling that reappears somewhere this cannot reach fails an
# assertion that names the stall instead of silently inverting a scenario.
#
# What the ceiling cost while it was there, which is why the assertion is
# worth its line: check-36 scenario B's "ComfyUI was handed this workflow
# exactly once" passed with worker_agent.py's ownership fence disabled. The
# reaped attempt was dying in ws.connect() before it ever reached the fence,
# so the assertion that exists to prove the fence works could not fail.
# ---------------------------------------------------------------------------
def _lift_ws_handshake_timeout() -> None:
    try:
        from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol
        from websockets.legacy.server import WebSocketServerProtocol
    except ImportError:  # no legacy implementation to patch
        return

    if not (isinstance(AutoWebSocketsProtocol, type)
            and issubclass(AutoWebSocketsProtocol, WebSocketServerProtocol)):
        return

    original_init = AutoWebSocketsProtocol.__init__

    def __init__(self, *args, **kwargs):
        # After the original, not through it: uvicorn calls super().__init__()
        # with an explicit keyword list that does not include open_timeout, so
        # there is no default to override on the way in.
        original_init(self, *args, **kwargs)
        self.open_timeout = None

    AutoWebSocketsProtocol.__init__ = __init__


_lift_ws_handshake_timeout()

app = FastAPI()
clients = {}
history = {}

# How long POST /prompt stalls before answering when the workflow carries the
# __slow_prompt__ marker (docs/10-roadmap.md, Q2). worker_agent.py's
# submit_prompt() blocks on this response, so this is the window in which
# ComfyUI HAS the workflow and the agent has not been told so yet — the window
# check-35-retry-doors.py kills an agent inside, to prove the phase breadcrumb
# already reads "executing" by then. Wide enough that "confirm ComfyUI has
# received it, read the breadcrumb, then kill" is not a race.
SLOW_PROMPT_DELAY_S = 6

# Every top-level node key of every workflow this stub has been handed, in
# arrival order, appended the moment /prompt is entered and BEFORE any stall.
#
# This is what makes "ComfyUI never saw this workflow" an assertion rather
# than an inference: a check gives its workflow a node key nothing else uses
# and then asks this stub directly whether it ever arrived. Both directions
# matter — check-30's retried job must NOT have reached here before the death
# that retried it, and check-35's cancelled job must NOT reach here at all.
received_nodes = []

# The filename_prefix this stub actually received for each node key that
# carried one, keyed by node id and overwritten on every /prompt that names
# that key again (docs/10-roadmap.md, Q3). worker_agent.py's
# scope_workflow_outputs() rewrites this input in place, inside the
# submitter's workspace, before the workflow is ever POSTed here — and this
# is the only way a check can prove that rewrite actually ran rather than
# merely that the job still completed: every job in this suite is reported
# through the identical {out_0001.png, ""} manifest (see run() below), so the
# outputs a check can see back from the gateway do not depend on this input
# at all and cannot tell a rewritten prefix from an unrewritten one.
received_filename_prefixes = {}


@app.get("/__received__")
async def received():
    return {"nodes": received_nodes}


@app.get("/__received_prefixes__")
async def received_prefixes_endpoint():
    return received_filename_prefixes


# One-shot: the next WebSocket connection stalls this long BEFORE the accept,
# leaving the agent parked in ws.connect() — after it has claimed the job and
# written its "dispatched" breadcrumb, and before it has sent ComfyUI anything
# at all. That is the window a pre-execution death actually lives in now that
# the "executing" breadcrumb is written ahead of the POST rather than after it,
# so it is the window check-30-sigkill.py has to kill an agent inside.
# Set through POST /__stall_next_ws__, consumed by the first connection to see
# it, and kept well under worker_agent.py's own 30s connect timeout.
#
# Not observable from in here, which is why the check asserts it by the clock
# instead. An abandoned handshake leaves this endpoint's coroutine running:
# it sleeps out the rest of the stall and its `await ws.accept()` returns
# normally onto a transport that closed while it was still sleeping, so a
# counter incremented after the accept reads the same in both worlds. The
# only party that can tell a stall from a drop is the client, and here the
# client is the agent under test — so the assertion lives in
# check-36-live-worker-fencing.py, as the wall-clock time the park actually
# lasted.
stall_next_ws_s = 0.0


@app.post("/__stall_next_ws__")
async def stall_next_ws(body: dict):
    global stall_next_ws_s
    stall_next_ws_s = float(body.get("seconds") or 0)
    return {"stall_next_ws_s": stall_next_ws_s}


# The {filename, subfolder} the NEXT completed job's /history manifest
# reports, instead of the default {out_0001.png, ""} every other check in
# this suite relies on being flat and identical across jobs. One-shot,
# consumed by run() and reset immediately after, so it never leaks into a
# check that does not ask for it.
#
# What this exists for (docs/10-roadmap.md, Q3 FIX 4a): output_subfolder()
# confines `subfolder`, but before FIX 4a the URL a browser was handed still
# concatenated the manifest's raw `filename` verbatim. Nothing else in this
# stub can produce a hostile filename — the default is a hardcoded literal —
# so a check proving that gap is closed has to be able to ask this stub to
# report one instead.
next_output_override = None


@app.post("/__set_next_output__")
async def set_next_output(body: dict):
    global next_output_override
    next_output_override = {
        "filename": body.get("filename", "out_0001.png"),
        "subfolder": body.get("subfolder", ""),
    }
    return next_output_override


@app.get("/system_stats")
async def stats():
    return {"system": {"comfyui_version": "stub"}}

@app.post("/prompt")
async def prompt(body: dict):
    wf = body.get("prompt") or {}
    # Before the rejection and before the stall: this endpoint being entered
    # at all is what "ComfyUI has been handed this workflow" means.
    received_nodes.extend(wf.keys())
    for node_id, node in wf.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict) and "filename_prefix" in inputs:
            received_filename_prefixes[node_id] = inputs["filename_prefix"]
    if "__fail__" in wf:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"message": "required input is missing: ckpt_name"}}, status_code=400)
    if "__slow_prompt__" in wf:
        await asyncio.sleep(SLOW_PROMPT_DELAY_S)
    pid = str(uuid.uuid4())
    asyncio.create_task(run(body.get("client_id"), pid, slow="__slow__" in wf,
                            vram_oom="__vram_oom__" in wf, die="__die__" in wf,
                            empty_frame="__empty_frame__" in wf,
                            close_after_history="__close_after_history__" in wf))
    return {"prompt_id": pid, "number": 1, "node_errors": {}}

@app.get("/history/{pid}")
async def hist(pid: str):
    return {pid: history[pid]} if pid in history else {}

@app.post("/interrupt")
async def interrupt():
    return {}

async def run(client_id, pid, slow=False, vram_oom=False, die=False,
              empty_frame=False, close_after_history=False):
    global next_output_override
    ws = clients.get(client_id)
    await asyncio.sleep(0.1)
    for i in range(1, 4):
        # An event for a DIFFERENT prompt, to prove prompt_id filtering works.
        await send(ws, {"type": "progress", "data": {"value": i, "max": 3, "prompt_id": "other-prompt"}})
        await send(ws, {"type": "progress", "data": {"value": i, "max": 3, "prompt_id": pid}})
        await asyncio.sleep(2.0 if slow else 0.05)

        if vram_oom and i == 2:
            # A VRAM exhaustion, as the real ComfyUI reports it: caught inside
            # the sampler, surfaced as execution_error on the socket, and the
            # server stays up and takes the next prompt. Nothing dies here.
            await send(ws, {"type": "execution_error", "data": {
                "prompt_id": pid,
                "node_id": "3",
                "node_type": "KSampler",
                "exception_type": "torch.OutOfMemoryError",
                "exception_message": (
                    "Allocation on device 0 would exceed allowed memory. "
                    "Tried to allocate 2.44 GiB. GPU 0 has a total capacity of "
                    "22.16 GiB of which 1.02 GiB is free."
                ),
            }})
            return

        if empty_frame and i == 2:
            # The frame websocket-client hands worker_agent.py when a server
            # closes the socket: recv() returns "" instead of raising (see
            # dying_ws below). Sent as a real empty text frame, and the
            # connection is deliberately LEFT OPEN afterwards, because that is
            # the only shape in which the agent's `if raw == ""` guard is
            # observable at all: close the TCP as well and the very next recv()
            # raises anyway, so a guard that is missing costs one loop
            # iteration instead of the whole job. Held open, a missing guard
            # means "" fails to parse, the loop continues, and the job sits
            # there holding a card until JOB_TIMEOUT -- 1800 seconds in
            # production. Nothing is written to /history, so the agent's one
            # look there finds nothing and it must fail the job NOW, naming the
            # connection.
            await send(ws, "")
            return

        if close_after_history and i == 2:
            # The other side of that door: the socket goes, but the work
            # actually landed. /history is populated FIRST and only then is the
            # connection closed -- with an ordinary code, so the agent's recv()
            # sees the empty frame -- and the terminal event is never sent. The
            # agent must ask /history rather than assume the worst, and report
            # the outputs it finds there.
            history[pid] = {"outputs": {"9": {"images": [
                {"filename": "out_0001.png", "subfolder": "", "type": "output"}]}}}
            if ws is not None:
                dying_ws[id(ws)] = 1000
            return

        if die and i == 2:
            # The host-RAM case, as far as this harness can carry it: the
            # ComfyUI PROCESS is gone, so its socket closes mid-job and its
            # /history never learns about this prompt. See check-70's docstring
            # for the half that is not reproducible here.
            clients.pop(client_id, None)
            # Ask the endpoint coroutine to return, which is what actually
            # closes the connection: calling ws.close() from this task does
            # not, while the endpoint is parked in its own await. Keyed on the
            # SOCKET, not the clientId -- a reconnecting agent can leave more
            # than one endpoint coroutine alive for the same id, and a
            # cid-keyed flag is then consumed by whichever polls first, which
            # closes an already-dead connection and leaves the live one open.
            if ws is not None:
                dying_ws[id(ws)] = 1006
            return

    filename, subfolder = "out_0001.png", ""
    if next_output_override is not None:
        filename = next_output_override.get("filename", filename)
        subfolder = next_output_override.get("subfolder", subfolder)
        next_output_override = None

    history[pid] = {"outputs": {"9": {"images": [
        {"filename": filename, "subfolder": subfolder, "type": "output"}]}}}

    # A foreign terminal event first: the naive agent ends the job here.
    await send(ws, {"type": "executing", "data": {"node": None, "prompt_id": "other-prompt"}})
    await asyncio.sleep(0.05)
    await send(ws, {"type": "executing", "data": {"node": None, "prompt_id": pid}})

async def send(ws, msg):
    """One frame to the agent. A msg of "" is sent verbatim as an empty text
    frame rather than as JSON, which is what `__empty_frame__` needs."""
    if ws:
        try:
            await ws.send_text("" if msg == "" else json.dumps(msg))
        except Exception:
            pass

# The WebSocket objects whose connection should be closed, keyed by id() and
# mapped to the close code to use. Set by run(); consumed by the endpoint
# below. The code is not decoration -- it decides what the AGENT's client
# library reports, and the two cases are genuinely different:
#
#   1006 is the reserved "abnormal closure" code, which a server may not put
#   on the wire: uvicorn's websockets layer refuses to serialize it, the
#   endpoint raises, and the TCP connection is simply dropped. websocket-client
#   then raises WebSocketConnectionClosedException out of recv(). This is
#   `__die__`, i.e. check-70's dead ComfyUI PROCESS.
#
#   1000/1001 are ordinary close codes and are sent as a real close frame.
#   websocket-client returns that frame from recv() as the EMPTY STRING rather
#   than raising -- which is the case worker_agent.py's `if raw == ""` guard
#   exists for, and the one no fixture reached before
#   check-75-closed-socket.py.
dying_ws: dict = {}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global stall_next_ws_s
    cid = ws.query_params.get("clientId")
    if stall_next_ws_s:
        stall, stall_next_ws_s = stall_next_ws_s, 0.0
        await asyncio.sleep(stall)
    await ws.accept()
    clients[cid] = ws
    try:
        while True:
            await asyncio.sleep(0.05)
            if id(ws) in dying_ws:
                code = dying_ws.pop(id(ws))
                clients.pop(cid, None)
                await ws.close(code=code)
                return
    except WebSocketDisconnect:
        clients.pop(cid, None)
