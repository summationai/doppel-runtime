"""What runs inside a PR's pod (checkpoint 5): the harness, one Codex process per reviewer run,
and the image it ships in. Imports only `doppel_shared`, and never the control plane: nothing in
the pod may hold the runtime's code, ledger or secrets.
"""
