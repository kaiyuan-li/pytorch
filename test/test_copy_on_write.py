# Owner(s): ["module: viewing and reshaping", "module: internals"]

import warnings

import pytest

import torch

def test_copy_on_write_warns():
    t = torch.ones(4)
    u = t.reshape(2, 2)

    # Writing any number of times to just one of the "views" is fine.
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        # Writing once to t is fine.
        t.add_(torch.arange(4))
        # Even writing twice to t is fine.
        t.add_(torch.ones(4))

    # But writing to (or reading from) u after we've written to t is a problem, because
    # u is still sharing t's storage.
    with pytest.warns(UserWarning, match='You have written through to both aliases created by calling reshape().'):
        u.add_(torch.ones(4).view(2, 2))

def test_copy_on_write():
    t = torch.ones(4)
    assert torch._C.get_copy_on_write_storage_generation(t) == 0
    assert torch._C.get_storage_generation(t) == 0

    u = t.reshape(2, 2)  # this will be a copy in the future
    assert not torch._C.has_same_copy_on_write_storage(t, u)
    assert torch._C.get_copy_on_write_storage_generation(u) == 0
    assert torch._C.get_storage_generation(u) == 0

    v = t.view(2, 2)
    assert torch._C.has_same_copy_on_write_storage(t, v)
    assert not torch._C.has_same_copy_on_write_storage(u, v)
    assert torch._C.get_copy_on_write_storage_generation(v) == 0
    assert torch._C.get_storage_generation(v) == 0

    # Write to t: t and u alias, so they both see bumps.
    t.add_(torch.ones(4))
    assert torch._C.get_copy_on_write_storage_generation(t) == 1
    assert torch._C.get_storage_generation(t) == 1
    assert torch._C.get_copy_on_write_storage_generation(v) == 1
    assert torch._C.get_storage_generation(v) == 1

    # But u will not alias in the future, so its copy-on-write generation will not
    # change.
    assert torch._C.get_copy_on_write_storage_generation(u) == 0
    assert torch._C.get_storage_generation(u) == 1
