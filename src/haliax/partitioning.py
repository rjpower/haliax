import contextlib
import functools
import threading
import typing
from math import prod
from typing import List, Mapping, Optional, Sequence, TypeVar, Union

import jax
from equinox import is_array
from equinox.custom_types import PyTree
from jax.experimental.pjit import pjit, with_sharding_constraint
from jax.interpreters.pxla import PartitionSpec

from .core import Axis, AxisSpec, NamedArray
from .util import StringHolderEnum, ensure_tuple, is_named_array


LogicalAxisName = str
PhysicalAxis = str
PhysicalAxisSpec = Union[PhysicalAxis, Sequence[PhysicalAxis]]
ResourceMapping = Mapping[LogicalAxisName, PhysicalAxisSpec]
"""Mapping from logical axis names to physical axis names"""


class ResourceAxis(StringHolderEnum):
    """Standard names for physical axes"""

    MODEL = "model"
    DATA = "data"


class _ResourceMappingHolder:
    """Global resource mapping, used with a context manager to give dynamic scoping to resource mappings"""

    def __init__(self):
        self.thread_data = threading.local()
        self.thread_data.resource_mapping = None


_mapping_holder = _ResourceMappingHolder()


@contextlib.contextmanager
def axis_mapping(mapping: ResourceMapping, **kwargs):
    """Context manager for setting the global resource mapping"""
    if len(kwargs):
        mapping = dict(mapping)
        mapping.update(kwargs)

    old_mapping = _mapping_holder.thread_data.resource_mapping
    _mapping_holder.thread_data.resource_mapping = mapping
    yield
    _mapping_holder.thread_data.resource_mapping = old_mapping


T = TypeVar("T", bound=PyTree)


def logically_sharded(x: T, logical_axes: Optional[PyTree] = None) -> T:
    """
    Shard a PyTree using the global resource mapping. NamedArrays in the PyTree are sharded using the resource mapping
     and the names in the tree. Non-NamedArrays are sharded using the logical_axes argument, if provided.

    If there is no global resource mapping, this function is a no-op.
    """
    mapping = _mapping_holder.thread_data.resource_mapping

    if mapping is None:
        return x

    def _as_pspec(x, logical_axis=None):
        if isinstance(x, NamedArray):
            physical_names: List[Optional[PhysicalAxisSpec]] = [mapping.get(a.name, None) for a in x.axes]
        elif logical_axis is not None:
            physical_names: List[Optional[PhysicalAxisSpec]] = [
                mapping.get(a, None) for a in ensure_tuple(logical_axis)
            ]
        elif is_array(x):
            physical_names = [None] * len(x.shape)
        else:
            return None

        spec = PartitionSpec(
            *tuple(tuple(p) if not (isinstance(p, str)) and isinstance(p, Sequence) else p for p in physical_names)
        )
        return spec

    if logical_axes is None:
        # TODO: support logical_axes as a tree prefix. jax doesn't seem to have a good utility for this.
        pspec = jax.tree_util.tree_map(_as_pspec, x, is_leaf=is_named_array)
    else:
        pspec = jax.tree_util.tree_map(_as_pspec, x, logical_axes, is_leaf=is_named_array)

    return with_sharding_constraint(x, pspec)


def infer_resource_partitions(tree: PyTree) -> PyTree:
    """
    Infer the resource partitions for a module, to be used with pjit.
    The basic idea is to tree all NamedArrays as leaves for the purposes of this function,
    and to create PartitionSpecs from those names plus the contextual resource_mapping.
    """

    def named_array_is_leaf(x):
        return isinstance(x, NamedArray)

    axis_resources = _mapping_holder.thread_data.resource_mapping

    if axis_resources is None:
        raise ValueError("No resource mapping found")

    def partition_spec(node: typing.Any):
        if isinstance(node, NamedArray):
            return NamedArray(
                PartitionSpec(*tuple(axis_resources.get(axis.name, None) for axis in node.axes)), node.axes
            )
        else:
            return None

    return jax.tree_util.tree_map(partition_spec, tree, is_leaf=named_array_is_leaf)


def eval_resource_partitions(fn):
    """
    Similar to jax.eval_shape but for resource partitions. It returns a PyTree of PartitionSpecs.
    """

    def f(*args, **kwargs):
        out_shape = jax.eval_shape(fn, *args, **kwargs)
        return infer_resource_partitions(out_shape)

    return f


def named_pjit_init(cls: typing.Type[T], **pjit_args):
    """Uses NamedArrays to infer the resource partitions for a module when creating it"""

    @functools.wraps(cls.__new__)
    def init(*args, **kwargs):
        inst = cls(*args, **kwargs)
        return inst

    return named_pjit(init, **pjit_args)


def named_pjit(fn=None, **pjit_args):
    """
    Uses NamedArrays to infer the resource partitions for calling a function
    """

    if fn is None:
        return functools.partial(named_pjit, **pjit_args)

    @functools.wraps(fn)
    def f(*args, **kwargs):
        in_resources = infer_resource_partitions((args, kwargs))
        shapes = jax.eval_shape(fn, *args, **kwargs)
        out_resources = infer_resource_partitions(shapes)

        @functools.wraps(fn)
        def fn_to_call(args, kwargs):
            return fn(*args, **kwargs)

        return pjit(fn_to_call, in_resources, out_resources, **pjit_args)(args, kwargs)

    return f


def physical_axis_name(axis: Axis) -> Optional[PhysicalAxis]:
    """Get the physical axis name for a logical axis"""
    mapping = _mapping_holder.thread_data.resource_mapping
    if mapping is None:
        return None
    else:
        return mapping.get(axis.name, None)


def physical_axis_size(axis: Axis) -> Optional[int]:
    """Get the physical axis size for a logical axis. This is the product of the size of all physical axes
    that this logical axis is mapped to."""
    # TODO: shouldn't be accessing this internal api, but...
    from jax.experimental.maps import thread_resources

    try:
        mesh_shape = thread_resources.env.shape
    except AttributeError:
        raise ValueError("No resource mapping found")

    name: Union[None, str, Sequence[str]] = physical_axis_name(axis)
    if name is None:
        return None
    elif isinstance(name, str):
        name = (name,)

    return prod([mesh_shape[n] for n in name])


def pspec_for_axis(axis: AxisSpec) -> PartitionSpec:
    """Get the PartitionSpec for a single axis"""
    axis = ensure_tuple(axis)
    return PartitionSpec(*(physical_axis_name(a) for a in axis))


def round_axis_for_partitioning(axis: Axis) -> Axis:
    """Round an axis so that it's divisible by the size of the partition it's on"""
    size = physical_axis_size(axis)
    if size is None:
        return axis
    else:
        new_size = (axis.size + size - 1) // size * size
        return Axis(axis.name, new_size)


__all__ = [
    "LogicalAxisName",
    "PhysicalAxis",
    "PhysicalAxisSpec",
    "ResourceAxis",
    "ResourceMapping",
    "axis_mapping",
    "logically_sharded",
    "infer_resource_partitions",
    "eval_resource_partitions",
    "named_pjit_init",
    "named_pjit",
    "physical_axis_name",
    "pspec_for_axis",
]
