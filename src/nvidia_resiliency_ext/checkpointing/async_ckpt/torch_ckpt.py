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

"""
TorchAsyncCheckpoint defines a wrapper for the async version of `torch.save` with
an additional method to synchronize async saving requests
"""


import logging
from typing import Optional
from nvidia_resiliency_ext.common.device_utils import get_xla_model
import torch
from ..utils import wrap_for_async, preload_tensors
from .core import AsyncCallsQueue, AsyncRequest

logger = logging.getLogger(__name__)

xm = get_xla_model()

class TorchAsyncCheckpoint(object):
    async_fn = None

    def __init__(self, group:Optional[torch.distributed.ProcessGroup]=None):
        self.group = group
        assert xm is None or torch.distributed.get_backend(group=self.group) == "gloo"
        self.save = torch.save
        self._async_calls_queue = AsyncCallsQueue(group=self.group)
        TorchAsyncCheckpoint.async_fn = wrap_for_async(torch.save)

    def async_save(self, state_dict, *args, **kwargs):
        """
        Keeps the original interface of `torch.save`
        Schedules a `AsyncReuqest` with preloading tensors to CPU with pinned memcpy
        """

        preloaded_sd = preload_tensors(state_dict)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        async_request = AsyncRequest(TorchAsyncCheckpoint.async_fn, (preloaded_sd, *args), [], kwargs, group=self.group)
        self._async_calls_queue.schedule_async_request(async_request)

    def finalize_async_save(self, blocking: bool=False, no_dist=True):
        """ Finalizes active async save calls.

        Args:
            blocking (bool, optional): if True, will wait until all active requests
                are done. Otherwise, finalizes only the async request that already
                finished. Defaults to False.
        """
        if blocking and self._async_calls_queue.get_num_unfinalized_calls() > 0:
            if torch.distributed.get_rank() == 0:
                logger.info('Unfinalized async checkpoint saves. Finalizing them synchronously now.')

        self._async_calls_queue.maybe_finalize_async_calls(blocking, no_dist=no_dist)
