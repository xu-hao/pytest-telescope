import pytest
from app.heavy import GROUPS, load

# telescope-scope: bundles=["imports"]


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_heavy_imports(group):
    assert load(group) == sorted(GROUPS[group])
