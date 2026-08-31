"""Stub ComfyUI: just enough API surface to exercise worker_agent.py."""
import asyncio, json, uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
clients = {}
history = {}

# How long POST /prompt stalls before answering when the workflow carries the
# __slow_prompt__ marker (docs/10-roadmap.md, Q2). worker_agent.py's
# submit_prompt() blocks on this response, so a check that wants to SIGKILL the
# agent while it is provably still waiting on ComfyUI's acceptance — i.e.
# before ComfyUI has ever seen the workflow — needs a window wide enough that
# "confirm the agent is already blocked in submit_prompt(), then kill it" is
# not a race. See check-30-sigkill.py.
SLOW_PROMPT_DELAY_S = 4

@app.get("/system_stats")
async def stats():
    return {"system": {"comfyui_version": "stub"}}

@app.post("/prompt")
async def prompt(body: dict):
    wf = body.get("prompt") or {}
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
    cid = ws.query_params.get("clientId")
    await ws.accept()
    clients[cid] = ws
    try:
        while True:
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        clients.pop(cid, None)
