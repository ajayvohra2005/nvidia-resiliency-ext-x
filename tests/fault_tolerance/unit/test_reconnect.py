# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
import random
import signal
import sys
import tempfile
import time

import pytest
import torch
import torch.multiprocessing as mp

from nvidia_resiliency_ext import fault_tolerance
from nvidia_resiliency_ext.fault_tolerance.data import FT_RANK_MONITOR_IPC_SOCKET_ENV_VAR
from nvidia_resiliency_ext.fault_tolerance.utils import wait_for_mp_events

from .utils import multiprocessing_execute_join, multiprocessing_execute_start

TEST_WORLD_SIZE = 4
ALL_RANK_IDS = set(range(TEST_WORLD_SIZE))
CHKPT_PATH = "/tmp/_ft_test_shutdown_dummy_chkpt.txt"
WORKLOAD_SHUTDOWN_TIMEOUT = 12
TERM_BY_FT_EXIT_CODE = 123
FT_TERM_SIGNAL = signal.SIGUSR1


def _get_ft_test_config():
    ft_cfg = fault_tolerance.FaultToleranceConfig()
    ft_cfg.initial_rank_heartbeat_timeout = 5.0
    ft_cfg.rank_heartbeat_timeout = 3.0
    ft_cfg.rank_termination_signal = FT_TERM_SIGNAL
    return ft_cfg


def _set_rmon_socket_env_var_for_this_rank():
    rank = os.environ["RANK"]
    ipc_sock_path = f"{tempfile.gettempdir()}/_rmon_r{rank}.socket"
    os.environ[FT_RANK_MONITOR_IPC_SOCKET_ENV_VAR] = ipc_sock_path


@pytest.fixture
def rank_monitors_fixture():
    ft_cfg = _get_ft_test_config()
    mp_ctx_spawn = mp.get_context("spawn")
    rank_monitors = []

    try:
        for rank in range(TEST_WORLD_SIZE):
            os.environ["RANK"] = str(rank)
            ipc_sock_path = f"{tempfile.gettempdir()}/_rmon_r{rank}.socket"
            p = fault_tolerance.RankMonitorServer.run_in_subprocess(
                cfg=ft_cfg,
                ipc_socket_path=ipc_sock_path,
                is_restarter_logger=False,
                mp_ctx=mp_ctx_spawn,
            )
            rank_monitors.append(p)
            os.environ["RANK"] = ''

        yield

    finally:
        for p in rank_monitors:
            with contextlib.suppress(Exception):
                p.terminate()
                p.join(timeout=180)


def _rank_main(*args, rank_ready_events, **kwargs):

    _set_rmon_socket_env_var_for_this_rank()
    rank_mon_cli = fault_tolerance.RankMonitorClient()
    rank_mon_cli.init_workload_monitoring()

    # Capture FT termination signal, and exit with custom code
    def _sig_handler(*args, **kwargs):
        sys.exit(TERM_BY_FT_EXIT_CODE)

    signal.signal(FT_TERM_SIGNAL, _sig_handler)

    # Notify main process that worker is initialized
    rank_ready_events[torch.distributed.get_rank()].set()

    # Infinite loop to simulate training
    while True:
        rank_mon_cli.send_heartbeat()
        time.sleep(1)
        try:
            torch.distributed.barrier()
        except Exception:
            # GLOO throws exception if distributed pg member is terminated
            # use sleep to simulate hang after some rank(s) are gone
            time.sleep(600)


def _send_sig(sig, pids):
    for pid in pids:
        os.kill(pid, sig)


def test_reconnect(rank_monitors_fixture):
    # Test idea:
    # - intialize rank monitors in fixture
    # Repeat few times:
    #   - run dummy ranks
    #   - kill some ranks
    #   - let FT terminate remaining ranks
    # Expected result: rank monitors should handle ranks re-connecting

    random.seed(123)

    for t in range(4):
        with contextlib.suppress(FileNotFoundError):
            os.remove(CHKPT_PATH)

        mp_ctx = torch.multiprocessing.get_context("spawn")
        rank_ready_events = [mp_ctx.Event() for _ in range(TEST_WORLD_SIZE)]

        rank_processes = multiprocessing_execute_start(
            worker_fn=_rank_main,
            world_size=TEST_WORLD_SIZE,
            mp_ctx=mp_ctx,
            backend="gloo",
            dist_store_type="file",
            test_scenario=[],
            rank_ready_events=rank_ready_events,
        )

        wait_for_mp_events(rank_ready_events, timeout=60)
        rank_pids = [p.pid for r, p in enumerate(rank_processes)]
        num_ranks_to_terminate = random.randint(1, len(rank_processes) - 1)
        target_pids = random.sample(rank_pids, num_ranks_to_terminate)
        expected_ret_codes = [0] * len(rank_processes)
        for r in range(len(rank_processes)):
            if rank_processes[r].pid in target_pids:
                expected_ret_codes[r] = -signal.SIGKILL
            else:
                expected_ret_codes[r] = TERM_BY_FT_EXIT_CODE

        time.sleep(4)
        _send_sig(signal.SIGKILL, target_pids)

        ret_codes = multiprocessing_execute_join(rank_processes, timeout=60)

        assert ret_codes == expected_ret_codes
