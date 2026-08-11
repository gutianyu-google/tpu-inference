# TPU steps on Kubernetes

Two ways to run a step on TPU. Pick by pod count, not by chip count.

## One pod

Name the queue. That is the whole interface.

```yaml
- label: "unit tests"
  agents:
    queue: v6e-8-2x4
  command: pytest tests/
```

The queue is bound to a TPU profile in `ci-infra`, and its agent-stack
controller supplies the Kueue queue label, node selector, chip count, TPU
toleration and job deadline. None of that belongs in this repo — a profile can
change region, reservation or chip count without touching any pipeline here.

Add `retry: automatic` on `signal_reason: agent_stop` and `exit_status: -1` if
the step runs on borrowed cohort capacity. Kueue preemption deletes the pod the
agent lives in, and Buildkite reads that as a lost agent.

## More than one pod

Multi-host slices (one pod per host, admitted together or not at all) and
prefill/decode disaggregation (separate server pods plus a benchmark pod) cannot
be a `batch/v1` Job, because a Job cannot span hosts. Declare a JobSet and hand
it to the launcher:

```yaml
- label: "disagg benchmark"
  agents:
    queue: jobset
  plugins:
    - kubernetes:
        podTemplate: tpu-launcher
  command: /opt/launcher/launch-jobset .buildkite/kubernetes/jobset_smoke.yaml
```

The launcher submits the JobSet, reports admission progress, streams pod logs
back into the step, and maps the JobSet result to the step's exit status. It
deletes the JobSet if the build is cancelled, and an `ownerReference` covers the
cases where it never gets the chance.

No `retry:` needed here. The agent runs on a CPU pod that Kueue does not manage,
so preemption evicts the JobSet without killing the build — it shows up as a
pause in the log, then a re-admission.

### Writing the manifest

The launcher fills in `metadata.name`, `namespace`, correlation labels and the
`ownerReference`. Everything else is yours, and unlike the single-pod path
nothing is injected — the manifest is the complete spec, including
`nodeSelector`, tolerations and TPU resources. See `jobset_smoke.yaml`.

Two things to get right:

- `kueue.x-k8s.io/queue-name` goes on the **JobSet**, not the inner Jobs.
- Set `activeDeadlineSeconds`. It is enforced inside the worker cluster, so it
  still bounds the run if the manager becomes unreachable.

`${VAR}` in the manifest is substituted from the step's environment, so
`${BUILDKITE_COMMIT}` works for pinning an image. To pass a literal `$` through
to the container — a shell variable the pod expands itself, like
`${JOB_COMPLETION_INDEX}` — double it: `$${JOB_COMPLETION_INDEX}`.

### Multi-host, when a pool exists

Current profiles (`v6e-1-1x1`, `v6e-8-2x4`) are both single-host, so a true
multi-host slice needs a new node pool first. When there is one, the shape is a
single `replicatedJob` with `parallelism` equal to the host count and
`completionMode: Indexed`; index 0 runs the driver and the rest join it.

For vLLM that means Ray: `TPU_MULTIHOST_BACKEND=ray` is the only multi-host
backend implemented, and its executor requires the process running the engine to
be on a node that has TPUs, with rank 0 pinned to that same node. So index 0 is
both the Ray head and a TPU worker — the same layout as the bare-metal script,
not a separate CPU head.
