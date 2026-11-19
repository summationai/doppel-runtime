from doppel_control_plane.core.payer import payer_for
from doppel_shared.testing import REPO_OWNER, STEVEN


def test_personal_reviewer_runs_on_its_owner_whatever_the_app_payer():
    assert payer_for(STEVEN, app_payer="key:summation") == "u-steven"


def test_app_reviewer_runs_on_the_app_payer():
    assert payer_for(REPO_OWNER, app_payer="key:summation") == "key:summation"
    assert payer_for(REPO_OWNER, app_payer=None) is None
