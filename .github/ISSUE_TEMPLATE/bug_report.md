---
name: Bug report
about: Something a script, manifest, or the gateway did wrong
labels: bug
---

**What happened, and what did you expect?**

**Which path are you on?**
<!-- PLATFORM=rosa or openshift · single-user or enterprise · STORAGE_MODE -->

**The step that failed, and its output**
```
# e.g. `make gpu`, `enterprise/setup.sh` — paste the failing section.
# Nothing here prints credentials, but skim before pasting anyway.
```

**If a pod is involved**
```
oc describe pod <pod> -n comfyui | sed -n '/Events/,$p'
oc logs -n comfyui <pod> --previous
```

**Before filing:** `docs/05-troubleshooting.md` is ordered by how likely you
are to hit each failure — worth 60 seconds. And `make preflight` output often
answers "is my account/quota/login the problem?" faster than we can.
