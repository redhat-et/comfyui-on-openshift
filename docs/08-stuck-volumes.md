# When a dead pod will not let go of the volume

The symptom: a pod dies, or a node disappears, and the replacement will not
start. `Multi-Attach error for volume`, or a mount that never completes, or a
pod sitting in `Terminating` for hours. The volume is held by something that no
longer exists.

```bash
./scripts/08-unstick-storage.sh            # diagnose, changes nothing
./scripts/08-unstick-storage.sh --repair   # fix, asking before anything destructive
```

The rest of this page is why it happens, because the obvious fix is the thing
that causes it.

## The rule

**A volume is released when the kubelet on the node that mounted it reports the
unmount. Nothing else releases it.** Not deleting the pod. Not deleting the
Deployment. Not deleting the PVC.

Everything below follows from that one sentence.

## Why `--force --grace-period=0` makes it permanent

This is in a lot of runbooks, including the one this repo grew out of:

> If AWS terminates a Spot Instance unexpectedly, run
> `oc delete pod -l app=... --force --grace-period=0` to clear the storage lock.

It does the opposite. Force-delete does not terminate anything — it deletes the
*API object* while the container may well still be running. The kubelet
therefore never unmounts, never reports the unmount, and you have destroyed the
only record of what still needs releasing. The volume is now stuck with nothing
left to point at.

If the node is alive, force-delete leaves a running container writing to a
filesystem Kubernetes believes is free — and then schedules a second pod onto
it. Two writers on one volume is how a model library gets corrupted.

Force-delete has one legitimate use: after the node has been confirmed gone and
tainted out-of-service, to clean up a pod record the control plane did not
reap. Never before.

## The two real causes

### The node died

Spot reclaim, hardware failure, a lost partition. There is no kubelet left to
report anything, so the control plane's position is "still mounted over there",
indefinitely.

The supported fix is a taint that says the node is genuinely gone and its
volumes may be force-detached:

```bash
oc adm taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
```

Volumes detach, pods on that node are deleted, replacements schedule elsewhere.
Remove it if the node ever comes back, or it will refuse to run anything:

```bash
oc adm taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-
```

**The taint is dangerous on a node that is still alive.** Force-detaching a
filesystem out from under a process that is writing to it corrupts data. This
is why it is not automatic, and why `08-unstick-storage.sh --repair` checks the
EC2 instance state through the AWS API before offering it — `Ready=False` on the
Node object means the kubelet stopped answering, which is not the same thing as
the machine having stopped running.

### The mount wedged

Rarer, more confusing, and specific to network storage.

NFS mounts default to `hard`, meaning an operation retries forever rather than
returning an error. That is the right default for data integrity. The
consequence is that if the filesystem becomes unreachable, every process
touching it enters uninterruptible sleep — and uninterruptible means SIGKILL
does not work. The pod cannot be killed. It sits in `Terminating`, the kubelet
cannot unmount, and the volume is never released.

The most common trigger is a reconnect after a brief network interruption. By
default the client comes back on a new source port and the server rejects it as
a different client; the mount does not fail, it hangs. `noresvport` lets the
reconnect succeed, and `scripts/04-storage.sh` now sets it on the EFS
StorageClass along with `timeo=600,retrans=2` to bound each retry.

**These are mount options, so they apply at mount time.** An existing PV keeps
the options it was created with. To pick them up:

```bash
oc get storageclass efs-sc -o jsonpath='{.mountOptions}'   # confirm they are there
oc delete deployment comfy-worker -n comfyui               # then let it recreate
```

If a node has a genuinely wedged mount and is otherwise healthy, no amount of
Kubernetes work will fix it — the stuck processes are in the kernel. Cordon it,
move the workload, and let the machine pool replace the node.

## Prevention, in the order that matters

**Give the pod time to shut down, and make it use that time.** The worker's
`terminationGracePeriodSeconds: 900` is only useful because the agent traps
SIGTERM, finishes the job in flight, and exits. A pod that is SIGKILLed at the
end of the grace period is a pod that never unmounted cleanly. This matters far
more here than in a normal deployment, because the worker pool scales to zero —
termination is routine, not exceptional.

**Reconsider spot for GPU workers.** GPU spot capacity is reclaimed often, and
every reclaim is a hard node death and another chance at this. The two-minute
warning is not enough to finish a generation. If the budget argument is
compelling, at least run the reclaim handler so pods drain rather than vanish.

**Prefer `Recreate` over `RollingUpdate`** for anything on a ReadWriteOnce
volume. Rolling brings up the new pod before the old one is gone, and with RWO
the new one cannot attach — you get a `Multi-Attach` error as a matter of
routine rather than as a failure. Both single-user and worker Deployments here
use `Recreate`.

**Use `Retain` for anything you would hate to lose.** The EFS StorageClass sets
it, so an accidentally deleted PVC leaves the data behind as a `Released` PV
rather than taking a 100 GB model library with it.

## Diagnosing by hand

```bash
# Which nodes are not answering
oc get nodes

# Volumes the control plane still believes are attached, and where
oc get volumeattachments -o custom-columns=\
'NAME:.metadata.name,NODE:.spec.nodeName,PV:.spec.source.persistentVolumeName,ATTACHED:.status.attached'

# Pods that will not die
oc get pods -A --field-selector=status.phase!=Succeeded \
  -o json | jq -r '.items[]|select(.metadata.deletionTimestamp)|"\(.metadata.namespace)/\(.metadata.name) \(.spec.nodeName)"'

# Why the replacement will not start
oc describe pod <pod> -n <ns> | sed -n '/Events/,$p'

# Is the machine actually dead, or just not talking?
aws ec2 describe-instances \
  --instance-ids "$(oc get node <node> -o jsonpath='{.spec.providerID}' | sed 's#.*/##')" \
  --query 'Reservations[0].Instances[0].State.Name' --output text
```

That last one is the question the whole thing turns on. `Ready=False` means the
kubelet stopped answering. `terminated` means the machine stopped running. Only
the second one makes force-detach safe.

## Sources

- [Detach volumes after non-graceful node shutdown — OpenShift](https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/storage/ephemeral-storage-csi-vol-detach-non-graceful-shutdown)
- [Non-graceful node shutdown GA — Kubernetes 1.28](https://kubernetes.io/blog/2023/08/16/kubernetes-1-28-non-graceful-node-shutdown-ga/)
- [aws-efs-csi-driver: hard mounts and unrecoverable hangs](https://github.com/kubernetes-sigs/aws-efs-csi-driver/issues/1827)
- [Troubleshoot Amazon EFS volume mount issues](https://repost.aws/knowledge-center/eks-troubleshoot-efs-volume-mount-issues)
