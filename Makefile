SHELL := /bin/bash
.DEFAULT_GOAL := help

# Everything real lives in scripts/. This file exists so the interface is short
# enough to remember without reading anything.

# Load .env here too, so the targets that call oc directly (forward, logs) see
# the same namespace as the scripts. The leading - makes it optional.
-include .env
export

.PHONY: help tools preflight account cluster gpu storage deploy logs forward \
        status park down destroy login up enterprise enterprise-down unstick \
        test lint push-models demo-local

help:
	@echo ""
	@echo "  ComfyUI on OpenShift — one command per step, run them in order."
	@echo ""
	@echo "  Setup"
	@echo "    make tools       install aws / rosa / oc / jq"
	@echo "    make preflight   check credentials, tools, quotas — safe, changes nothing"
	@echo "    make account     file GPU quota requests + budget alarm  (run this FIRST,"
	@echo "                     the quota approval is the multi-day long pole)"
	@echo ""
	@echo "  Bring it up"
	@echo "    make cluster     ROSA HCP cluster + GPU machine pool     (~20 min)"
	@echo "    make gpu         NFD + NVIDIA GPU Operator + smoke test  (~20 min)"
	@echo "    make storage     model + output volumes"
	@echo "    make deploy      build and deploy ComfyUI (single user, one pod)"
	@echo "    make up          all four of the above, in order"
	@echo ""
	@echo "  Or, instead of deploy: the multi-user configuration"
	@echo "    make enterprise  queue + gateway + GPU pool that scales 0..N"
	@echo "                     needs STORAGE_MODE=rwx — see enterprise/README.md"
	@echo ""
	@echo "  Develop"
	@echo "    make test        unit + e2e suites: real Redis, stub ComfyUI — no cluster, ~1 min"
	@echo "    make demo-local  the real pool on this machine's own GPU: gateway, queue,"
	@echo "                     N real workers rendering — no cluster, no AWS  (DEMO_WORKERS=2)"
	@echo "    make lint        everything CI checks: shellcheck, syntax, py, manifests"
	@echo ""
	@echo "  Use it"
	@echo "    make login       print the oc login command"
	@echo "    make status      what is running and what it costs"
	@echo "    make forward     port-forward ComfyUI to localhost:8188"
	@echo "    make logs        tail the ComfyUI pod"
	@echo "    make push-models rsync a local model dir into the cluster (SRC=./models)"
	@echo "    make unstick     a dead pod is holding a volume — diagnose and repair"
	@echo ""
	@echo "  Stop paying"
	@echo "    make park        GPU pool to 0 replicas    ~\$$2.04/hr -> ~\$$1.06/hr"
	@echo "    make down        delete the cluster        ~\$$2.04/hr -> ~\$$0.05/hr"
	@echo "    make destroy     delete everything incl. VPC and IAM"
	@echo ""
	@echo "  Already have an OpenShift cluster? Set PLATFORM=openshift in .env,"
	@echo "  log in with oc, then: make gpu storage deploy"
	@echo ""

tools:
	@scripts/00-tools.sh

preflight:
	@scripts/00-preflight.sh

account:
	@scripts/01-bootstrap-account.sh

cluster:
	@scripts/02-cluster.sh

gpu:
	@scripts/03-gpu-operators.sh

storage:
	@scripts/04-storage.sh

deploy:
	@scripts/05-deploy.sh

# Sequential on purpose — each step needs the previous one finished, and they
# each take long enough that you want to see which one failed.
up: cluster gpu storage deploy

enterprise:
	@enterprise/setup.sh

enterprise-down:
	@enterprise/teardown.sh

status:
	@scripts/06-status.sh

unstick:
	@scripts/08-unstick-storage.sh --repair

# Fastest first: the pytest layer (in-process, no dependencies beyond pytest
# itself and what hub.py/worker_agent.py already import), then the shell
# units (instant, no dependencies), then the e2e suite (needs redis-server
# and the pip deps — see CONTRIBUTING.md).
test:
	@python3 -m pytest enterprise/test/unit -q
	@scripts/unit-tests.sh
	@enterprise/test/run.sh

lint:
	@scripts/lint.sh

demo-local:
	@enterprise/demo-local.sh

push-models:
	@scripts/10-push-models.sh $(SRC)

login:
	@scripts/07-login.sh

forward:
	@echo "ComfyUI at http://localhost:8188 — ctrl-c to stop"
	@oc port-forward -n $${APP_NAMESPACE:-comfyui} svc/comfyui 8188:8188

logs:
	@oc logs -n $${APP_NAMESPACE:-comfyui} -l app=comfyui -f --tail=200

park:
	@scripts/99-teardown.sh park

down:
	@scripts/99-teardown.sh cluster

destroy:
	@scripts/99-teardown.sh all
