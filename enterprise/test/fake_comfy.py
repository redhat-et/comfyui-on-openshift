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


@app.get("/__received__")
async def received():
    return {"nodes": received_nodes}


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


@app.get("/system_stats")
async def stats():
    return {"system": {"comfyui_version": "stub"}}

@app.post("/prompt")
async def prompt(body: dict):
    wf = body.get("prompt") or {}
    # Before the rejection and before the stall: this endpoint being entered
    # at all is what "ComfyUI has been handed this workflow" means.
    received_nodes.extend(wf.keys())
    if "__fail__" in wf:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": {"message": "required input is missing: ckpt_name"}}, status_code=400)
    if "__slow_prompt__" in wf:
        await asyncio.sleep(SLOW_PROMPT_DELAY_S)
    pid = str(uuid.uuid4())
    asyncio.create_task(run(body.get("client_id"), pid, slow="__slow__" in wf))
    return {"prompt_id": pid, "number": 1, "node_errors": {}}

@app.get("/history/{pid}")
async def hist(pid: str):
    return {pid: history[pid]} if pid in history else {}

@app.post("/interrupt")
async def interrupt():
    return {}

async def run(client_id, pid, slow=False):
    ws = clients.get(client_id)
    await asyncio.sleep(0.1)
    for i in range(1, 4):
        # An event for a DIFFERENT prompt, to prove prompt_id filtering works.
        await send(ws, {"type": "progress", "data": {"value": i, "max": 3, "prompt_id": "other-prompt"}})
        await send(ws, {"type": "progress", "data": {"value": i, "max": 3, "prompt_id": pid}})
        await asyncio.sleep(2.0 if slow else 0.05)

    history[pid] = {"outputs": {"9": {"images": [
        {"filename": "out_0001.png", "subfolder": "", "type": "output"}]}}}

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
