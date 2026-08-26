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
and a role in the namespace can reach it; nobody else can. This is the right
answer for a team.

The enterprise configuration already implements this pattern properly —
`enterprise/manifests/05-oauth-proxy.yaml` and `05-oauth-proxy-patch.yaml` are
the maintained reference. What follows is the same pattern applied to the
single-user Deployment, and **all four pieces are load-bearing**:

1. **Rebind ComfyUI to loopback.** The image's CMD is `--listen 0.0.0.0`, so
   without an override, anything in the cluster can reach 8188 directly and
   skip the login. A pod-spec `args` replaces the image CMD *wholesale*, so
   the override must carry every flag, not just `--listen`.
2. **Switch the probes to `exec`.** Once ComfyUI binds loopback the kubelet
   cannot reach it over HTTP; an exec probe runs inside the pod's network
   namespace and can.
3. **Expose only the proxy's port on the Service.** A Service that still lists
   8188 is a bypass, loopback or not for other ports.
4. **URL-safe session secret.** oauth-proxy decodes the cookie secret with
   base64.URLEncoding when it needs AES; standard base64's `+` and `/` fail
   that decode if `--pass-access-token` or `--cookie-refresh` are ever added.

```yaml
# manifests/base/deployment.yaml — changes inside the pod spec
      serviceAccountName: comfyui
      containers:
        - name: comfyui
          # Replaces the image CMD entirely: every flag from app/Containerfile,
          # with --listen changed to loopback.
          args:
            - --listen
            - 127.0.0.1
            - --port
            - "8188"
            - --models-directory
            - /models
            - --output-directory
            - /output
            - --temp-directory
            - /tmp
          # The kubelet cannot reach a loopback port; exec probes can.
          startupProbe:
            httpGet: null
            exec:
              command: ["python3", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/',timeout=5)"]
            periodSeconds: 10
            failureThreshold: 90
          readinessProbe:
            httpGet: null
            exec:
              command: ["python3", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/',timeout=5)"]
            periodSeconds: 10
          livenessProbe:
            httpGet: null
            exec:
              command: ["python3", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/',timeout=10)"]
            periodSeconds: 30
            failureThreshold: 3

        - name: oauth-proxy
          image: registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:latest
          ports:
            - name: public
              containerPort: 8443
          args:
            - --provider=openshift
            - --https-address=:8443
            - --http-address=
            - --openshift-service-account=comfyui
            - --upstream=http://127.0.0.1:8188
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

      volumes:
        # ...existing models/output/scratch volumes stay...
        - name: proxy-tls
          secret:
            secretName: comfyui-proxy-tls
        - name: proxy-secret
          secret:
            secretName: comfyui-proxy-secret
```

```yaml
# manifests/base/service.yaml — the proxy port REPLACES 8188.
# Leaving 8188 in this list would let anything in the cluster skip the login.
spec:
  ports:
    - name: public
      port: 8443
      targetPort: public
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

# URL-safe base64, no padding — see point 4 above
oc create secret generic comfyui-proxy-secret -n comfyui \
  --from-literal=session_secret="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"

# expose 8443, not 8188
oc create route reencrypt comfyui -n comfyui --service=comfyui --port=public
```

The key detail: after the `args` override, ComfyUI listens only on
`127.0.0.1:8188` inside the pod, and the Service only knows about the proxy's
8443. The proxy is the sole way in, so there is no bypass — from outside the
cluster or from within it.

## Option 3 — private Route, VPN only

If your VPC is peered to a corporate network, create the cluster with
`--private` and use an internal Route. No public ingress at all. Cheaper than a
public NLB and simpler than the proxy, if you already have the network path.

## What not to do

- `oc expose svc/comfyui` with no proxy. This is the one that gets people.
- Relying on an obscure hostname. Route hostnames are predictable and certificate
  transparency logs publish them within minutes.
- ComfyUI's `--enable-cors-header` as a security control. It is not one.
