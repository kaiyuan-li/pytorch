# Owner(s): ["oncall: distributed"]

import os
import sys

import torch
import torch.distributed as dist
from os import path

torch.backends.cuda.matmul.allow_tf32 = False

if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

from torch.testing._internal.common_utils import run_tests, TEST_WITH_DEV_DBG_ASAN, NO_MULTIPROCESSING_SPAWN
from torch.testing._internal.distributed.distributed_test import (
    DistributedTest, TestDistBackend
)

if TEST_WITH_DEV_DBG_ASAN:
    print("Skip dev-asan as torch + multiprocessing spawn have known issues", file=sys.stderr)
    sys.exit(0)

if NO_MULTIPROCESSING_SPAWN:
    print("Spawn not available, skipping tests.", file=sys.stderr)
    sys.exit(0)

_allowed_backends = ("gloo", "nccl", "ucc")
if (
    "BACKEND" not in os.environ
    or "WORLD_SIZE" not in os.environ
    or "TEMP_DIR" not in os.environ
    or not path.exists(path.join(os.environ["TEMP_DIR"], "barrier"))
):
    # TODO can we actually have `run_tests.py` emit the complete instructions when it prints a repro command?
    raise RuntimeError(
        "Missing expected env vars for `test_distributed_spawn.py`.  Please ensure to specify the following:\n"
        f"'BACKEND' = one of {_allowed_backends}\n"
        f"'WORLD_SIZE' = int >= 2\n"
        "'TEMP_DIR' specifying a directory containing a barrier file named 'barrier'.\n\n"
        f"e.g.\ntouch /tmp/barrier && TEMP_DIR=/tmp BACKEND='nccl' WORLD_SIZE=2 python {__file__}",
    )

BACKEND = os.environ["BACKEND"]

if BACKEND in _allowed_backends:
    def make_test_classes():
        class TestDistBackendWithSpawn(TestDistBackend, DistributedTest._DistTestBase):
            def setUp(self):
                super().setUp()
                self._spawn_processes()
                torch.backends.cudnn.flags(enabled=True, allow_tf32=False).__enter__()
        has_init_method = "NoInitFile" if os.environ.get("INIT_METHOD", "env://") == "env://" else ""
        name = f"TestDistBackendWithSpawn{BACKEND.upper()}{has_init_method}"
        empty_class = type(name, (TestDistBackendWithSpawn,), {})

        globals()[name] = empty_class
        return empty_class
    make_test_classes()
else:
    print(f"Invalid backend {BACKEND}. Tests will not be run!")


if __name__ == "__main__":
    run_tests()
