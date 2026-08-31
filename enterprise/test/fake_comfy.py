"""Stub ComfyUI: just enough API surface to exercise worker_agent.py."""
import asyncio, json, uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

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
                            vram_oom="__vram_oom__" in wf, die="__die__" in wf))
    return {"prompt_id": pid, "number": 1, "node_errors": {}}

@app.get("/history/{pid}")
async def hist(pid: str):
    return {pid: history[pid]} if pid in history else {}

@app.post("/interrupt")
async def interrupt():
    return {}

async def run(client_id, pid, slow=False, vram_oom=False, die=False):
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

        if die and i == 2:
            # The host-RAM case, as far as this harness can carry it: the
            # ComfyUI PROCESS is gone, so its socket closes mid-job and its
            # /history never learns about this prompt. See check-70's docstring
            # for the half that is not reproducible here.
            clients.pop(client_id, None)
            try:
                await ws.close(code=1006)
            except Exception:
                pass
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
    if ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass

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
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        clients.pop(cid, None)
