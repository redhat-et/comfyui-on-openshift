# Storage

You called this the tricky part. It is, and the reason is worth stating plainly:
**the models are the problem, not the volumes.** A single SDXL checkpoint is
~7 GB. A working ComfyUI install with a couple of checkpoints, VAEs, LoRAs,
upscalers and ControlNets is 40–120 GB. Provisioning a volume is easy;
deciding where those bytes live so that a cluster rebuild does not mean a
two-hour re-download is the actual design question.

## The three options

### `STORAGE_MODE=rwo` — gp3 block volume (the default)

One EBS gp3 volume, `ReadWriteOnce`, mounted by exactly one pod on one node.

- ~$0.08/GB-month. 100 GiB is ~$8/month.
- Fast. Sequential reads at gp3 baseline throughput, no network hop, no NFS
  protocol overhead. Loading a 7 GB checkpoint into VRAM is limited by the PCIe
  bus, not the storage.
- **Dies with the cluster.** `make down` destroys it.

This is the right default and you should stay here. One GPU supports one ComfyUI
pod, so `ReadWriteMany` buys you nothing that you can use.

### `STORAGE_MODE=rwx` — EFS

An EFS filesystem with a CSI access point per PVC, `ReadWriteMany`, mountable by
many pods across many nodes.

- ~$0.30/GB-month — roughly 4× gp3. 100 GB is ~$30/month.
- Slower for large sequential reads. NFS over the network, and elastic
  throughput mode meters what you pull. A cold checkpoint load is noticeably
  worse than gp3.
- **Survives the cluster.** This is the real reason to pick it. `make down`
  leaves the models sitting there, and the rebuilt cluster mounts the same
  filesystem.

`scripts/04-storage.sh` automates the whole setup, which on an STS cluster is
eight things that all have to be right simultaneously:

1. An IAM role trusted by the cluster's OIDC provider, with a condition binding
   it to two specific service accounts in `openshift-cluster-csi-drivers`.
2. An inline policy with the `elasticfilesystem:*` actions the driver needs,
   including the tag conditions on access point create/delete.
3. The `aws-efs-csi-driver-operator` subscription.
4. An `aws-efs-cloud-credentials` secret containing `role_arn` — omit this and
   the controller starts happily and then fails every provisioning call with
   `AccessDenied`, which is a confusing way to discover a missing secret.
5. A `ClusterCSIDriver` CR for `efs.csi.aws.com`.
6. The EFS filesystem itself.
7. A security group allowing TCP 2049 from the VPC CIDR.
8. A mount target in every private subnet the nodes live in.

Getting six of eight right produces a PVC stuck in `Pending` with no useful
event. That is why it is scripted.

The script assumes an AWS-hosted cluster — EFS is an AWS service. On a
non-AWS OpenShift cluster (`PLATFORM=openshift` on bare metal, vSphere, …),
skip `make storage` in rwx mode: provision whatever RWX storage that platform
offers (ODF, an NFS provisioner), and create `comfyui-models` and
`comfyui-output` PVCs with `accessModes: [ReadWriteMany]` yourself. On an
AWS-hosted BYO cluster whose VPC the script cannot discover by name, set
`VPC_ID` in `.env`.

### S3 + a sync job — the pragmatic middle (scripted)

Keep gp3 for speed, keep the canonical models in S3, and sync on startup.

- ~$0.023/GB-month for the S3 copy. 100 GB is ~$2/month.
- Cluster rebuild pulls from S3 at ~100 MB/s inside the region, so 100 GB is
  ~15 minutes of unattended init container rather than hours over the internet.
- The moving parts — bucket, a read-only IAM role trusted by the cluster's
  OIDC provider, an annotated ServiceAccount, the init container — are all
  scripted.

```bash
scripts/09-s3-models.sh                  # bucket + IAM role + ServiceAccount, once
# put MODELS_S3_BUCKET=<bucket> in .env
aws s3 sync ./models "s3://<bucket>/"    # upload with your own credentials
make deploy                              # pod now syncs the bucket on start
```

The role the pod gets is deliberately read-only: pods pull models, and
uploading stays on your machine with your own credentials — a compromised pod
cannot corrupt the canonical store.

## Choosing

| | gp3 (rwo) | EFS (rwx) | S3 + sync |
|---|---|---|---|
| $/month for 100 GB | ~$8 | ~$30 | ~$2 + gp3 |
| Model load speed | best | worst | best |
| Survives `make down` | no | yes | yes |
| Multiple pods | no | yes | no |
| Setup complexity | none | high (scripted) | medium (scripted) |

**Start with gp3.** Move to EFS if you find yourself rebuilding clusters often
enough that the re-download hurts, or if you actually grow a second consumer.
Move to S3 if you want both speed and durability and do not mind the init
container.

## Loading models in the first place

```bash
# from your machine — works whether or not a ComfyUI pod is running
# (SRC mirrors the volume layout: SRC/checkpoints, SRC/loras, ...)
make push-models SRC=./models

# or by hand, into the running pod
POD=$(oc get pod -n comfyui -l app=comfyui -o name | head -1)
oc rsync ./checkpoints "${POD#pod/}":/models/checkpoints -n comfyui

# or from inside the pod, straight from Hugging Face
oc rsh -n comfyui "$POD"
  cd /models/checkpoints && curl -fLO https://huggingface.co/.../model.safetensors
```

The second is much faster — it pulls at datacenter bandwidth rather than through
your home upload. It also runs through the NAT gateway at $0.045/GB, so a 100 GB
model library costs about $4.50 in transit. Worth knowing, not worth optimizing.

## Permissions

If a write to `/models` fails with `Permission denied`, it is the arbitrary-UID
problem, not the volume. OpenShift runs your container as a random high UID with
GID 0 supplementary. The volume is fine; the mount point in the image needs to
be group-writable and group-root-owned. See the bottom of `app/Containerfile`.

For EFS specifically, the access point in the StorageClass sets
`directoryPerms: "775"` and a GID range, which is what makes the arbitrary UID
able to write. If you change those, change them knowing why.
