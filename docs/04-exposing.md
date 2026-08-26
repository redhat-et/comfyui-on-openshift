# Letting other people reach it

The default is `make forward` — an authenticated port-forward through the
cluster API to `localhost:8188`. No Route, nothing listening on the internet.

That is deliberate. **ComfyUI has no authentication, and its custom-node system
executes arbitrary Python by design.** A plain `oc expose svc/comfyui` puts a
remote code execution endpoint on the public internet, attached to a node that
holds cloud credentials and can reach your VPC. There is no configuration inside
ComfyUI that fixes this; the fix has to be in front of it.

## Option 1 — port-forward (default)

```bash
make forward
```

Authenticated by your `oc` session. Fine for one person. Dies when your terminal
does.

## Option 2 — oauth-proxy sidecar

Puts OpenShift's own login in front of ComfyUI. Anyone with a cluster account
can reach it; nobody else can. This is the right answer for a team.

```yaml
# add to the pod spec in manifests/base/deployment.yaml
      serviceAccountName: comfyui
      containers:
        - name: oauth-proxy
          image: registry.redhat.io/openshift4/ose-oauth-proxy:latest
          ports:
            - name: public
              containerPort: 8443
          args:
            - --provider=openshift
            - --https-address=:8443
            - --http-address=
            - --openshift-service-account=comfyui
            - --upstream=http://localhost:8188
            - --tls-cert=/etc/tls/private/tls.crt
            - --tls-key=/etc/tls/private/tls.key
            - --cookie-secret-file=/etc/proxy/secrets/session_secret
            # Only users who can 'get' this namespace get in.
            - --openshift-sar={"namespace":"comfyui","resource":"namespaces","verb":"get"}
          volumeMounts:
            - name: proxy-tls
              mountPath: /etc/tls/private
            - name: proxy-secret
              mountPath: /etc/proxy/secrets
```

plus:

```bash
oc create sa comfyui -n comfyui

oc annotate sa comfyui -n comfyui \
  serviceaccounts.openshift.io/oauth-redirectreference.primary=\
'{"kind":"OAuthRedirectReference","apiVersion":"v1","reference":{"kind":"Route","name":"comfyui"}}'

# service-serving cert, issued and rotated by the cluster
oc annotate svc comfyui -n comfyui \
  service.beta.openshift.io/serving-cert-secret-name=comfyui-proxy-tls

oc create secret generic comfyui-proxy-secret -n comfyui \
  --from-literal=session_secret="$(head -c 32 /dev/urandom | base64)"

# expose 8443, not 8188
oc create route reencrypt comfyui -n comfyui --service=comfyui --port=public
```

The key detail: ComfyUI still listens only on `localhost:8188` inside the pod.
The proxy is the only thing bound to a port the Service can reach, so there is
no bypass.

## Option 3 — private Route, VPN only

If your VPC is peered to a corporate network, create the cluster with
`--private` and use an internal Route. No public ingress at all. Cheaper than a
public NLB and simpler than the proxy, if you already have the network path.

## What not to do

- `oc expose svc/comfyui` with no proxy. This is the one that gets people.
- Relying on an obscure hostname. Route hostnames are predictable and certificate
  transparency logs publish them within minutes.
- ComfyUI's `--enable-cors-header` as a security control. It is not one.
