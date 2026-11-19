"""The gateway: the one door out of a pod (DSV-8).

    model_route   /model/{path}: capability -> run -> payer -> credential, budget, token counting
    (checkpoint 6) the GitHub posting route, on the internal listener only

Imports only `doppel_shared`: it learns about runs through the `Runs` port and about sign-ins
through the `Credentials` port, never from the control plane's ledger or the broker's files.
"""

from doppel_gateway.model_route import MODEL_MAX_BYTES, ModelRoute

__all__ = ["MODEL_MAX_BYTES", "ModelRoute"]
