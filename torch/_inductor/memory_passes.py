# pyre-strict

from typing import List, Dict

import torch

from . import config, ir, scheduler
from .utils import contains_collective, contains_wait, tuple_sorted, sympy_product
import logging
from .virtualized import V
from collections import defaultdict

torch_log = logging.getLogger("torch")


def get_users_from_unfused_nodes(snode):
    # if a fused node has 2 subnodes (A, B) and each subnode has 2 users (A1, A2) and (B1, B2),
    # this function returns (A1, A2, B1, B2).
    if isinstance(snode, scheduler._BaseGroupedSchedulerNode):
        return list(set([user for snode in snode.snodes for user in snode.users]))
    else:
        return snode.users


def _collect_reads(snode):
    if isinstance(snode, scheduler._BaseGroupedSchedulerNode):
        return [dep for snode in snode.snodes for dep in snode.read_writes.reads]
    else:
        return snode.read_writes.reads


def _collect_writes(snode):
    if isinstance(snode, scheduler._BaseGroupedSchedulerNode):
        return [dep for snode in snode.snodes for dep in snode.read_writes.writes]
    else:
        return snode.read_writes.writes


def raise_last_usage(
    name_to_fused_node: Dict[str, "scheduler.BaseSchedulerNode"], graph_inputs: Dict[str, "Buffer"], snodes: List["scheduler.BaseSchedulerNode"]
) -> List["scheduler.BaseSchedulerNode"]:
    """
    For each node, we move its consumer nodes earlier if it satisfies the following conditions:
        - Assuming the consumer node X's input args have write sites W1, W2 before X in the original schedule,
          then W1 and W2 have to be already scheduled in the new schedule before we can consider scheduling X.
          (This is to ensure the value for those input args remains correct after reordering.)
        - The consumer node only writes to one output tensor.
        - The consumer node's out tensor is smaller than the sum memory of all its last-usage input args.
    If we found a consumer node of current node that satisfies the above conditions, we can schedule it right after the current node.
    After moving these consumer nodes up, we are able to immediately release the memory of their last-usage input args.
    """

    write_map = defaultdict(set)  # bufX -> set of nodes that write to bufX (including itself)
    node_to_input_prev_writes = defaultdict(set)  # nodeY -> set of writes nodes to nodeY's input args before nodeY
    for snode in snodes:
        for dep in _collect_writes(snode):
            write_map[dep.name].add(snode.get_name())
        if isinstance(snode.node, ir.ResizeStorageBytes):
            write_map[snode.node.resized_buf_name].add(snode.get_name())
        for dep in _collect_reads(snode):
            node_to_input_prev_writes[snode].update(write_map[dep.name])
            node_to_input_prev_writes[snode].discard(snode.get_name())
            if isinstance(snode, scheduler._BaseGroupedSchedulerNode):
                for sub_snode in snode.snodes:
                    node_to_input_prev_writes[sub_snode].update(write_map[dep.name])
                    node_to_input_prev_writes[sub_snode].discard(sub_snode.get_name())

    def get_numel_by_name(buf_name):
        def _compute_elems(buf):
            if isinstance(buf.layout, ir.MultiOutputLayout):
                users = [user for user in name_to_fused_node[buf.get_name()].node_users if user.get_name() != buf.get_name()]
                return sum(_compute_elems(user.node) for user in users)
            else:
                return V.graph.sizevars.size_hint(sympy_product(buf.get_size()))

        if buf_name in graph_inputs:
            buf = graph_inputs[buf_name]
            return _compute_elems(buf)
        else:
            snode = name_to_fused_node[buf_name]
            if isinstance(snode, scheduler.FusedSchedulerNode):
                return sum(_compute_elems(sub_snode.node) for sub_snode in snode.snodes)
            else:
                buf = snode.node
                return _compute_elems(buf)

    new_order = []
    scheduled = set()
    scheduled.update(graph_inputs.keys())
    for snode in snodes:
        if snode.get_name() not in scheduled:
            new_order.append(snode)
            scheduled.add(snode.get_name())
            if isinstance(snode, scheduler._BaseGroupedSchedulerNode):
                for sub_snode in snode.snodes:
                    scheduled.add(sub_snode.get_name())
        for user in get_users_from_unfused_nodes(snode):
            # Can't early release `snode` if it's needed by OutputNode
            if isinstance(user.node, scheduler.OutputNode):
                break
            if user.node.get_name() in scheduled:
                continue
            if contains_collective(user.node) or contains_wait(user.node):
                continue
            # For now, we don't move users that are MultiOutput
            if isinstance(user.node.node, ir.MultiOutput):
                continue
            # For now, we don't move users that are GroupedSchedulerNode
            if isinstance(name_to_fused_node[user.node.get_name()], scheduler.GroupedSchedulerNode):
                continue
            # For now, we don't move users that have GroupedSchedulerNode as input
            if any((not x.name in graph_inputs and isinstance(name_to_fused_node[x.name], scheduler.GroupedSchedulerNode)) for x in user.node.read_writes.reads):
                continue
            # For now, we don't move users that are run_and_save_rng_state ops
            if isinstance(user.node.node, ir.ExternKernel) and user.node.node.op_overload is torch.ops.higher_order.run_and_save_rng_state:
                continue
            if (
                all(prev_write in scheduled for prev_write in node_to_input_prev_writes[user.node])
                # and len(user.node.read_writes.writes) == 1 and list(user.node.read_writes.writes)[0].name == user.node.get_name()
                and (
                    # if raising the user node saves memory
                    get_numel_by_name(user.node.get_name()) < sum(get_numel_by_name(x_name) for x_name in user.node.last_usage)
                    or (
                        # always profitable to raise resize-to-0
                        isinstance(user.node.node, ir.ResizeStorageBytes)
                        and user.node.node.constant_args[0] == 0
                    )
                    # always okay to raise nop kernel
                    or isinstance(user.node, scheduler.NopKernelSchedulerNode)
                )
            ):
                user_node = name_to_fused_node[user.node.get_name()]
                new_order.append(user_node)
                scheduled.add(user_node.get_name())
    return new_order


def raise_primal_resize_zero_if_primal_is_unused(
    name_to_fused_node: Dict[str, "scheduler.BaseSchedulerNode"], graph_inputs: Dict[str, "Buffer"], snodes: List["scheduler.BaseSchedulerNode"]
) -> List["scheduler.BaseSchedulerNode"]:
    primal_to_reads = defaultdict(set)  # argX -> set of nodes that reads argX
    primal_resize_zero_nodes = []
    for snode in snodes:
        for dep in _collect_reads(snode):
            if dep.name.startswith("arg"):
                primal_to_reads[dep.name].add(snode.get_name())
    for snode in snodes:
        if (
            isinstance(snode.node, ir.ResizeStorageBytes)
            and snode.node.constant_args[0] == 0
            and snode.node.resized_buf_name in graph_inputs
            and len(primal_to_reads[snode.node.resized_buf_name]) == 1
            and list(primal_to_reads[snode.node.resized_buf_name])[0] == snode.get_name()
        ):
            primal_resize_zero_nodes.append(snode)
    new_order = primal_resize_zero_nodes
    scheduled = set(x.get_name() for x in new_order)
    for snode in snodes:
        if snode.get_name() not in scheduled:
            new_order.append(snode)
            scheduled.add(snode.get_name())
    return new_order