# Your code goes here

`src/` is copied into the image at `/opt/comfyui/`. It is empty on purpose —
drop your ComfyUI backend, custom nodes, and workflows in and rebuild.

```
app/
├── Containerfile              the image; read the comments before changing it
├── requirements-extra.txt     extra pip deps, installed at build time
└── src/
    ├── custom_nodes/          your nodes land in ComfyUI's custom_nodes/
    ├── workflows/             saved workflow JSON
    └── ...                    anything else that belongs in the ComfyUI tree
```

## Two rules that will save you an afternoon

**Install at build time, never at start.** The container runs as an arbitrary
UID with no write access outside the mounted volumes, so a custom node that
pip-installs on first import will fail. Put its dependencies in
`requirements-extra.txt`.

**Anything you write to must be a volume or group-writable.** `/models`,
`/output`, and `/tmp` are mounted and safe. Anywhere else in the image needs the
`chgrp 0` + `chmod g=u` treatment the Containerfile applies at the bottom — if
you add a new writable directory, add it to that list too.

## Building

In-cluster, which is the path of least resistance and needs no local podman:

```bash
# leave COMFYUI_IMAGE empty in .env
make deploy
```

Locally, if you would rather push to a registry you control:

```bash
podman build -t quay.io/you/comfyui:latest -f app/Containerfile app/
podman push quay.io/you/comfyui:latest
# then set COMFYUI_IMAGE=quay.io/you/comfyui:latest in .env
make deploy
```

## Models

Models are not baked into the image — they live on the `comfyui-models` volume
so the image stays small and a model change does not mean a rebuild. To load
them:

```bash
make forward           # then use ComfyUI's own model manager, or:
oc rsync ./checkpoints comfyui-<pod>:/models/checkpoints -n comfyui
```

For anything you would hate to re-download, see `docs/03-storage.md` — either
`STORAGE_MODE=rwx` (models outlive the cluster) or an S3 sync job.
