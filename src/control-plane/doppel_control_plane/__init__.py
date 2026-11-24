"""The control plane: the runtime process, a runner for Doppelwerk (docs/specs/bff-split.md).

    bff          pairing, seats and check-ins with Doppelwerk
    jobs         the jobs Doppelwerk hands this runner, run in the sandbox
    pods, mirrors  one pod per PR; one bare mirror per repo, a checkout per head
    store/       the ledger: the jobs in hand, and what the gateway counts
    runs         the `Runs` port on the ledger, for the gateway
    roles        the four roles' lifecycle in the process
    app, cli, config   the process: listeners, wiring of gateway and model provider

Inside the control plane, `store` imports only `doppel_shared`; the rest may import any of it, plus `doppel_gateway`
and `doppel_model_provider`, which it runs in-process.
"""

__version__ = "0.2.0"
