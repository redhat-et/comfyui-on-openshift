# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). Please do not open a public issue
for anything exploitable.

## What counts

Two different kinds of finding land here, and both are welcome:

- **Code vulnerabilities** — in the gateway (`enterprise/gateway/hub.py`),
  the worker agent, or the scripts. The gateway is the deliberate attack
  surface: it accepts workflow JSON from users and serves files from a shared
  volume, so path handling and input validation bugs there matter most.
- **Deployment-pattern weaknesses** — a manifest or script in this repo that
  configures something insecurely by default. The security model this repo
  promises is documented in `docs/04-exposing.md` and
  `docs/06-enterprise-architecture.md` (short version: raw ComfyUI is never
  network-reachable; the GPU pods bind loopback and have no Service or
  Route). Anything that quietly breaks that promise is a vulnerability, not
  a nitpick.

## Out of scope

ComfyUI itself and its custom-node ecosystem are upstream projects — report
their vulnerabilities upstream. This repo's mitigation for that entire class
is architectural: keep ComfyUI unreachable and `ENABLE_MANAGER=false`.
