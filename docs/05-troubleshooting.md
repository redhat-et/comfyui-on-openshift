# Troubleshooting

Ordered by how likely you are to hit it.

## A dead pod is holding the volume

`Multi-Attach error`, a mount that never completes, or a pod in `Terminating`
for hours after a node died.

```bash
./scripts/08-unstick-storage.sh --repair
```

**Do not reach for `oc delete pod --force --grace-period=0`.** It deletes the
pod record while the container may still be running, which strands the volume
permanently instead of releasing it. `docs/08-stuck-volumes.md` has the full
explanation and the supported fix.

## GPU pool stuck in Provisioning forever

Two different causes that look identical.

**Quota.** `make preflight` catches this. If G/VT vCPUs is 0, no amount of
waiting helps.

```bash
aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA
aws service-quotas list-requested-service-quota-change-history --status PENDING
```

**Capacity.** The quota is fine and AWS simply has no `g6.xlarge` free in that
AZ. Offerings say the type exists there; they say nothing about capacity.

```bash
rosa describe machinepool --cluster "$CLUSTER_NAME" gpu
oc get events -A --field-selector reason=FailedCreate
```

Switching region is faster than waiting. `us-west-2` and `us-east-1` have the
deepest G-family pools; `us-east-2` is cheaper when it has stock. Changing
`GPU_INSTANCE_TYPE` to a different family (`g5.xlarge` instead of `g6.xlarge`)
also works — they draw from separate capacity pools.

## ClusterPolicy never becomes ready

```bash
oc get clusterpolicy -o yaml | grep -A5 status
oc get pods -n nvidia-gpu-operator
oc logs -n nvidia-gpu-operator -l app=nvidia-driver-daemonset --tail=100
```

**If it has been under 20 minutes, it is not stuck.** The driver container
compiles against the running RHCOS kernel and pulls several GB. First run on a
fresh node is genuinely slow.

**`nvidia-driver-daemonset` in `Init` or `ImagePullBackOff`** — usually the node
cannot reach `nvcr.io`. Check the NAT gateway exists and the private route table
points at it.

**No driver pods scheduled at all** — NFD did not label the node. Check:

```bash
oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true
oc get pods -n openshift-nfd
```

If NFD is running and the label is missing, the node has no NVIDIA card — you
are looking at a base worker, not the GPU pool.

## ComfyUI pod is CrashLoopBackOff

Almost always the arbitrary UID.

```bash
oc logs -n comfyui -l app=comfyui --previous
```

`Permission denied` on any path is the tell. OpenShift assigned your container
a random high UID with GID 0 supplementary; the path it is writing to is not
group-writable. The fix is in the image, not the manifest:

```dockerfile
RUN chgrp -R 0 /the/path && chmod -R g=u /the/path
```

`OSError: [Errno 30] Read-only file system` means the same thing about a path
that is not a volume at all. Mount it or move the write.

`ModuleNotFoundError` at start means a custom node tried to install its own
dependencies at import time. Add them to `app/requirements-extra.txt` and
rebuild.

## Pod stays Pending

```bash
oc describe pod -n comfyui -l app=comfyui | sed -n '/Events/,$p'
```

- `0/3 nodes are available: 3 Insufficient nvidia.com/gpu` — the GPU node is not
  ready or the operator has not advertised capacity yet. `oc get nodes -o
  custom-columns='N:.metadata.name,G:.status.capacity.nvidia\.com/gpu'`
- `node(s) had untolerated taint {nvidia.com/gpu: true}` — your pod is missing
  the toleration. The manifests here have it; anything you wrote yourself does
  not.
- `pod has unbound immediate PersistentVolumeClaims` — see below.

## PVC stuck in Pending

```bash
oc describe pvc -n comfyui comfyui-models
oc get storageclass
```

**`rwo` mode**: the default StorageClass name differs by platform (`gp3-csi` on
ROSA, often `gp3` on self-managed). `04-storage.sh` resolves it from the
cluster's default-class annotation, so a failure here usually means there is no
default StorageClass at all. Set one:

```bash
oc patch storageclass <name> -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

**`rwx` mode**: almost always the credentials secret or a missing mount target.

```bash
oc logs -n openshift-cluster-csi-drivers -l app=aws-efs-csi-driver-controller -c csi-provisioner
```

`AccessDenied` → the `aws-efs-cloud-credentials` secret is missing, has the
wrong key, or the IAM role's trust policy does not name both service accounts.
The secret must carry a `credentials` key holding an AWS config file
(`[default]` + `role_arn` + `web_identity_token_file`) — a bare `role_arn` key
is silently ignored and produces exactly this symptom. Check with:

```bash
oc get secret aws-efs-cloud-credentials -n openshift-cluster-csi-drivers \
  -o jsonpath='{.data.credentials}' | base64 -d
```
Timeout on mount → no mount target in the node's subnet, or the security group
does not allow 2049 from the VPC CIDR.

## Build fails

```bash
oc logs -n comfyui bc/comfyui
```

`OOMKilled` — the torch install needs headroom. Raise the BuildConfig memory
limit in `scripts/05-deploy.sh` above 8Gi.

`no space left on device` — the build node's ephemeral storage. Build on the
base pool (the default) rather than the GPU node, or build locally and push to a
registry, setting `COMFYUI_IMAGE` in `.env`.

## rosa create cluster fails immediately

- `ERR: Failed to verify AWS support plan` — you are on Basic or Developer.
  See `docs/01-aws-account.md` — AWS lists Business+ as a prerequisite, Red Hat
  calls it recommended, and which of those you are hitting depends on the CLI
  version.
- `ERR: --installer-role-arn is required` on `rosa create operator-roles` — the
  account roles step did not produce an HCP installer role. Re-run
  `rosa create account-roles --mode auto --hosted-cp --force-policy-creation --yes`
  and check `rosa list account-roles` shows one with `HCP` in the name.
- `ERR: Insufficient quota` — run `make account` and wait.
- `ERR: The AWS account is not linked to a Red Hat account` — you skipped
  enabling ROSA at `console.aws.amazon.com/rosa`, or the Red Hat account behind
  `ROSA_TOKEN` is not the one you linked.
- Operator role errors after re-creating a cluster with the same name — the old
  OIDC config is stale. `rosa delete operator-roles --prefix "$CLUSTER_NAME"
  --mode auto` and re-run.

## Everything works but generation is slow

Check you are actually on the GPU:

```bash
oc rsh -n comfyui deploy/comfyui nvidia-smi
```

If `nvidia-smi` shows 0% utilization during a generation, torch fell back to
CPU — usually a CUDA/torch version mismatch after editing the Containerfile.
`python3 -c "import torch; print(torch.cuda.is_available())"` inside the pod
tells you in one line.

If utilization is high and it is still slow, it is the card. An L4 is roughly
half an A10G for fp16 diffusion. `GPU_INSTANCE_TYPE=g5.xlarge` or `g6e.xlarge`
and `make cluster` again.
