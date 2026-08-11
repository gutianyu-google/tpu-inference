# TPU steps on Kubernetes

One path for everything: the launcher submits the workload and streams it back.

```yaml
- label: "unit tests"
  agents:
    queue: kube
  plugins:
    - kubernetes:
        podTemplate: tpu-launcher
  command: /opt/launcher/launch --profile v6e-8-2x4 -- pytest tests/
```

The step names a **profile**. Placement, chip count, topology, the Kueue queue
and the run deadline all come from the cluster-side profile registry, generated
in `ci-infra` from the same tfvars that creates the node pools and the Kueue
queues. Nothing about Kubernetes placement lives in this repo, and a profile
cannot change region, reservation or chip count without the queues following.

Run `/opt/launcher/launch --profile bogus -- true` to see the available
profiles; an unknown profile or template fails immediately with the list.

## Why a launcher even for one pod

agent-stack-k8s can only create a `batch/v1` Job, and a Job cannot span hosts,
so multi-host slices and prefill/decode disagg need a launcher regardless.
Routing single-pod work through it too costs one cheap CPU pod and buys two
things, both of which depend on the agent living *outside* the Kueue workload:

- **No `retry:` for preemption.** Kueue evicts the workload; the agent is on a
  CPU pod it does not manage, so the step log pauses and resumes instead of the
  build failing with a lost agent.
- **No reservation races.** The agent acquires its Buildkite job in seconds
  rather than after admission and node pool scale-up, so the job is never held
  reserved long enough to be claimed twice.

## Templates

`--template` picks the workload shape; the default is `job`.

| Template | Shape |
|---|---|
| `job` | one pod, one host (default) |
| `jobset-multihost` | one pod per host, gang admitted, Ray bootstrap env |

```yaml
command: /opt/launcher/launch --profile v6e-8-2x4 --template jobset-multihost -- bash bench.sh
```

Templates live in the cluster, not here, so an opinionated shape is defined
once. Adding one — a `jobset-1p1d` for prefill/decode, say — is a file under
`kueue/launcher/templates/` in `ci-infra`.

## Images

The image is the pipeline's choice, not the cluster's — real CI images are
built per commit, so the cluster cannot know it. Set it once at pipeline level:

```yaml
env:
  WORKLOAD_IMAGE: "us-central1-docker.pkg.dev/.../vllm:${BUILDKITE_COMMIT}"
```

There is deliberately no cluster-side default. A step that forgot its image
would otherwise run whatever the default happened to be and could pass, which
is worse than failing — so the launcher fails immediately instead.

Because the image is repo-controlled, and in a public repo that means
PR-controlled, the launcher checks it against `allowed_image_repos` from the
cluster-side registry before submitting. An image outside those prefixes is
rejected.

## Multi-host

Both current profiles are single-host, so `jobset-multihost` needs a
multi-host node pool and a profile with `hosts` set before it does anything
useful.

When there is one: index 0 runs the command and the other hosts join it. For
vLLM that means Ray — `TPU_MULTIHOST_BACKEND=ray` is the only multi-host
backend implemented, and its executor requires the engine process to sit on a
TPU node with rank 0 pinned to that same node. So index 0 is both Ray head and
TPU worker, matching the bare-metal layout rather than using a separate CPU
head. The head address comes from JobSet's stable DNS, which is what replaces
the IP discovery in `run_multihost.sh`.
