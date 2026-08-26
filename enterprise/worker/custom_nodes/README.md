# Custom nodes for the GPU worker image

Anything here is baked into the worker image and available on every GPU pod.

This is the durable path, and in an autoscaled pool it is the *only* durable
path: the worker pool scales to zero, so nodes installed at runtime through
ComfyUI-Manager live exactly as long as the pod does.

`enterprise/setup.sh` copies `app/src/custom_nodes/` in here before building, so
keep one copy of your nodes in `app/src/custom_nodes/` and both the single-user
and the enterprise images pick them up.
