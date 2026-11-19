"""The control plane: the runtime process (docs/specs/doppel-service.md).

    core/        pure decisions: command parsing, payer choice, slot matching, the merger
    store/       the ledger and its schema
    scheduler    queues slots, starts ready ones in their PR's pod, runs the merger
    runs         the `Runs` port on the ledger, for the gateway
    roles        the five roles' lifecycle in the process
    app, cli, config   the process: listeners, wiring of gateway and model provider

Inside the control plane, `core` and `store` import only `doppel_shared`; the rest may import any
of them, plus `doppel_gateway` and `doppel_model_provider`, which it runs in-process.
"""

__version__ = "0.1.0"
