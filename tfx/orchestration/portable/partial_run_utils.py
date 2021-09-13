# Copyright 2021 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Portable library for partial runs."""

import collections
import enum
from typing import Any, Callable, Collection, List, Mapping, MutableMapping, Optional, Set, Tuple, Union

from absl import logging
from tfx.dsl.compiler import compiler_utils
from tfx.dsl.compiler import constants
from tfx.orchestration import metadata
from tfx.orchestration.portable import execution_publish_utils
from tfx.orchestration.portable.mlmd import context_lib
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import pipeline_pb2

from google.protobuf import any_pb2
from ml_metadata.proto import metadata_store_pb2

_default_chief_settings = pipeline_pb2.NodeExecutionOptions.ChiefSettings()
_default_chief_settings.latest_pipeline_run_strategy.SetInParent()


def mark_pipeline(
    pipeline: pipeline_pb2.Pipeline,
    from_nodes: Callable[[str], bool] = lambda _: True,
    to_nodes: Callable[[str], bool] = lambda _: True,
    chief_settings: pipeline_pb2.NodeExecutionOptions
    .ChiefSettings = _default_chief_settings,
):
  """Modifies the Pipeline IR in place, in preparation for partial run.

  This function modifies the node-level execution_options to annotate them with
  additional information needed for partial runs, such as which nodes to run,
  which nodes to skip, which node is responsible for additional bookkeeping
  (the "chief" node), and settings for that bookkeeping entails.

  The set of nodes included in the filtered pipeline is the set of nodes between
  from_nodes and to_nodes -- i.e., the set of nodes that are reachable by
  traversing downstream from `from_nodes` AND also reachable by traversing
  upstream from `to_nodes`.

  Args:
    pipeline: A valid compiled Pipeline IR proto to be marked.
    from_nodes: A predicate function that selects nodes by their ids. The set of
      nodes whose node_ids return True determine where the "sweep" starts from
      (see detailed description).
      Defaults to lambda _: True (i.e., select all nodes).
    to_nodes: A predicate function that selects nodes by their ids. The set of
      nodes whose node_ids return True determine where the "sweep" ends (see
      detailed description).
      Defaults to lambda _: True (i.e., select all nodes).
    chief_settings: Settings needed by the chief node to perform the bookkeeping
      step. Defaults to using LATEST_PIPELINE_RUN strategy.

  Raises:
    ValueError: If pipeline's execution_mode is not SYNC.
    ValueError: If pipeline contains a sub-pipeline.
    ValueError: If pipeline is not topologically sorted.
  """
  _ensure_sync_pipeline(pipeline)
  _ensure_no_subpipeline_nodes(pipeline)
  _ensure_topologically_sorted(pipeline)

  node_map = _make_ordered_node_map(pipeline)
  from_node_ids = [node_id for node_id in node_map if from_nodes(node_id)]
  to_node_ids = [node_id for node_id in node_map if to_nodes(node_id)]
  node_map = _filter_node_map(node_map, from_node_ids, to_node_ids)
  node_map, excluded_direct_dependencies = _fix_nodes(node_map)
  _mark_nodes_and_nominate_chief(
      pipeline,
      node_ids_to_include=set(node_map.keys()),
      excluded_direct_dependencies=excluded_direct_dependencies,
      chief_settings=chief_settings)


def _mark_nodes_and_nominate_chief(
    pipeline: pipeline_pb2.Pipeline, node_ids_to_include: Set[str],
    excluded_direct_dependencies: Set[str],
    chief_settings: pipeline_pb2.NodeExecutionOptions.ChiefSettings):
  """Mark nodes and nominate chief."""
  chief_already_nominated = False
  for node in pipeline.nodes:  # assumes topological order
    node_id = node.pipeline_node.node_info.id
    node_exec_opts = node.pipeline_node.execution_options
    if node_id in node_ids_to_include:
      if chief_already_nominated:
        node_exec_opts.run.SetInParent()
      else:
        node_exec_opts.run.chief_settings.CopyFrom(chief_settings)
        chief_already_nominated = True
    else:
      node_exec_opts.skip.child_in_partial_run = (
          node_id in excluded_direct_dependencies)


class _Direction(enum.Enum):
  UPSTREAM = 1
  DOWNSTREAM = 2


def _ensure_sync_pipeline(pipeline: pipeline_pb2.Pipeline):
  """Raises ValueError if the pipeline's execution_mode is not SYNC."""
  if pipeline.execution_mode != pipeline_pb2.Pipeline.SYNC:
    raise ValueError('Pipeline filtering is only supported for '
                     'SYNC pipelines.')


def _ensure_no_subpipeline_nodes(pipeline: pipeline_pb2.Pipeline):
  """Raises ValueError if the pipeline contains a sub-pipeline.

  If the pipeline comes from the compiler, it should already be
  flattened. This is just in case the IR proto was created in another way.

  Args:
    pipeline: The input pipeline.

  Raises:
    ValueError: If the pipeline contains a sub-pipeline.
  """
  for pipeline_or_node in pipeline.nodes:
    if pipeline_or_node.HasField('sub_pipeline'):
      raise ValueError(
          'Pipeline filtering not supported for pipelines with sub-pipelines. '
          f'sub-pipeline found: {pipeline_or_node}')


def _ensure_topologically_sorted(pipeline: pipeline_pb2.Pipeline):
  """Raises ValueError if nodes are not topologically sorted.

  If the pipeline comes from the compiler, it should already be
  topologically sorted. This is just in case the IR proto was modified or
  created in another way.

  Args:
    pipeline: The input pipeline.

  Raises:
    ValueError: If the pipeline is not topologically sorted.
  """
  # Upstream check
  visited = set()
  for pipeline_or_node in pipeline.nodes:
    node = pipeline_or_node.pipeline_node
    for upstream_node in node.upstream_nodes:
      if upstream_node not in visited:
        raise ValueError(
            'Input pipeline is not topologically sorted. '
            f'node {node.node_info.id} has upstream_node {upstream_node}, but '
            f'{upstream_node} does not appear before {node.node_info.id}')
    visited.add(node.node_info.id)
  # Downstream check
  visited.clear()
  for pipeline_or_node in reversed(pipeline.nodes):
    node = pipeline_or_node.pipeline_node
    for downstream_node in node.downstream_nodes:
      if downstream_node not in visited:
        raise ValueError(
            'Input pipeline is not topologically sorted. '
            f'node {node.node_info.id} has downstream_node {downstream_node}, '
            f'but {downstream_node} does not appear after {node.node_info.id}')
    visited.add(node.node_info.id)


def _make_ordered_node_map(
    pipeline: pipeline_pb2.Pipeline
) -> 'collections.OrderedDict[str, pipeline_pb2.PipelineNode]':
  """Prepares the Pipeline proto for DAG traversal.

  Args:
    pipeline: The input Pipeline proto, which must already be topologically
      sorted.

  Returns:
    An OrderedDict that maps node_ids to PipelineNodes.
  """
  result = collections.OrderedDict()
  for pipeline_or_node in pipeline.nodes:
    node_id = pipeline_or_node.pipeline_node.node_info.id
    result[node_id] = pipeline_or_node.pipeline_node
  return result


def _traverse(node_map: Mapping[str, pipeline_pb2.PipelineNode],
              direction: _Direction, start_nodes: Collection[str]) -> Set[str]:
  """Traverses a DAG from start_nodes, either upstream or downstream.

  Args:
    node_map: Mapping of node_id to nodes.
    direction: _Direction.UPSTREAM or _Direction.DOWNSTREAM.
    start_nodes: node_ids to start from.

  Returns:
    Set of node_ids visited by this traversal.
  """
  result = set()
  stack = []
  for start_node in start_nodes:
    # Depth-first traversal
    stack.append(start_node)
    while stack:
      current_node_id = stack.pop()
      if current_node_id in result:
        continue
      result.add(current_node_id)
      if direction == _Direction.UPSTREAM:
        stack.extend(node_map[current_node_id].upstream_nodes)
      elif direction == _Direction.DOWNSTREAM:
        stack.extend(node_map[current_node_id].downstream_nodes)
  return result


def _filter_node_map(
    node_map: 'collections.OrderedDict[str, pipeline_pb2.PipelineNode]',
    from_node_ids: Collection[str],
    to_node_ids: Collection[str],
) -> 'collections.OrderedDict[str, pipeline_pb2.PipelineNode]':
  """Returns an OrderedDict with only the nodes we want to include."""
  ancestors_of_to_nodes = _traverse(node_map, _Direction.UPSTREAM, to_node_ids)
  descendents_of_from_nodes = _traverse(node_map, _Direction.DOWNSTREAM,
                                        from_node_ids)
  nodes_to_keep = ancestors_of_to_nodes.intersection(descendents_of_from_nodes)
  result = collections.OrderedDict()
  for node_id, node in node_map.items():
    if node_id in nodes_to_keep:
      result[node_id] = node
  return result


def _remove_dangling_downstream_nodes(
    node: pipeline_pb2.PipelineNode,
    node_ids_to_keep: Collection[str]) -> pipeline_pb2.PipelineNode:
  """Removes node.downstream_nodes that have been filtered out."""
  # Using a loop instead of set intersection to ensure the same order.
  downstream_nodes_to_keep = [
      downstream_node for downstream_node in node.downstream_nodes
      if downstream_node in node_ids_to_keep
  ]
  if len(downstream_nodes_to_keep) == len(node.downstream_nodes):
    return node
  result = pipeline_pb2.PipelineNode()
  result.CopyFrom(node)
  result.downstream_nodes[:] = downstream_nodes_to_keep
  return result


def _handle_missing_inputs(
    node: pipeline_pb2.PipelineNode,
    node_ids_to_keep: Collection[str],
) -> Tuple[pipeline_pb2.PipelineNode, Set[str]]:
  """Handles missing inputs.

  Args:
    node: The Pipeline node to check for missing inputs.
    node_ids_to_keep: The node_ids that are not filtered out.

  Returns:
    A Tuple containing two elements:
    - A copy of the Pipeline node with some upstream_nodes removed,
    - The set of excluded node_ids that are producer nodes of the nodes to keep.
  """
  upstream_nodes_removed = set()
  upstream_nodes_to_keep = []
  for upstream_node in node.upstream_nodes:
    if upstream_node in node_ids_to_keep:
      upstream_nodes_to_keep.append(upstream_node)
    else:
      upstream_nodes_removed.add(upstream_node)

  if not upstream_nodes_removed:
    return node, set()  # No parent missing, no need to change anything.

  excluded_direct_deps = set()
  new_node = pipeline_pb2.PipelineNode()
  new_node.CopyFrom(node)
  for input_spec in new_node.inputs.inputs.values():
    for channel in input_spec.channels:
      if channel.producer_node_query.id in upstream_nodes_removed:
        excluded_direct_deps.add(channel.producer_node_query.id)
  new_node.upstream_nodes[:] = upstream_nodes_to_keep
  return new_node, excluded_direct_deps


def _fix_nodes(
    node_map: 'collections.OrderedDict[str, pipeline_pb2.PipelineNode]',
) -> Tuple['collections.OrderedDict[str, pipeline_pb2.PipelineNode]', Set[str]]:
  """Removes dangling references and handle missing inputs."""
  fixed_nodes = collections.OrderedDict()
  merged_excluded_direct_deps = set()
  for node_id in node_map:
    new_node = _remove_dangling_downstream_nodes(
        node=node_map[node_id], node_ids_to_keep=node_map.keys())
    new_node, excluded_direct_deps = _handle_missing_inputs(
        node=new_node, node_ids_to_keep=node_map.keys())
    fixed_nodes[node_id] = new_node
    merged_excluded_direct_deps |= excluded_direct_deps
  return fixed_nodes, merged_excluded_direct_deps


def _fix_deployment_config(
    input_pipeline: pipeline_pb2.Pipeline,
    node_ids_to_keep: Collection[str]) -> Union[any_pb2.Any, None]:
  """Filters per-node deployment configs.

  Cast deployment configs from Any proto to IntermediateDeploymentConfig.
  Take all three per-node fields and filter out the nodes using
  node_ids_to_keep. This works because those fields don't contain references to
  other nodes.

  Args:
    input_pipeline: The input Pipeline IR proto.
    node_ids_to_keep: Set of node_ids to keep.

  Returns:
    If the deployment_config field is set in the input_pipeline, this would
    output the deployment config with filtered per-node configs, then cast into
    an Any proto. If the deployment_config field is unset in the input_pipeline,
    then this function would return None.
  """
  if not input_pipeline.HasField('deployment_config'):
    return None

  deployment_config = pipeline_pb2.IntermediateDeploymentConfig()
  input_pipeline.deployment_config.Unpack(deployment_config)

  def _fix_per_node_config(config_map: MutableMapping[str, Any]):
    for node_id in list(config_map.keys()):  # make a temporary copy of the keys
      if node_id not in node_ids_to_keep:
        del config_map[node_id]

  _fix_per_node_config(deployment_config.executor_specs)
  _fix_per_node_config(deployment_config.custom_driver_specs)
  _fix_per_node_config(deployment_config.node_level_platform_configs)

  result = any_pb2.Any()
  result.Pack(deployment_config)
  return result


def snapshot(pipeline_node: pipeline_pb2.PipelineNode,
             mlmd_connection_config: metadata.ConnectionConfigType,
             pipeline: pipeline_pb2.Pipeline):
  """Performs a snapshot if pipeline_node is chief, else no-op.

  Args:
    pipeline_node: If this is the chief node, performs the snapshot. Otherwise,
      does nothing.
    mlmd_connection_config: Used for connecting to the MLMD where the snapshot
      is to be performed.
    pipeline: The full pipeline.

  Raises:
    ValueError: If pipeline_node has a chief_settings field set, but
      artifact_reuse_strategy field is not set in it.
  """
  if not pipeline_node.execution_options.run.HasField('chief_settings'):
    logging.info(
        'Node %s is not chief. '
        'Assuming that chief node has prepared the necessary dependencies.',
        pipeline_node.node_info.id)
    return

  logging.info('Node %s is chief.', pipeline_node.node_info.id)
  chief_settings = pipeline_node.execution_options.run.chief_settings
  logging.info('chief_settings: %s', chief_settings)
  if chief_settings.HasField('base_pipeline_run_strategy'):
    base_run_id = chief_settings.base_pipeline_run_strategy.base_run_id
    logging.info('Using base_pipeline_run_strategy with base_run_id=%s',
                 base_run_id)
  elif chief_settings.HasField('latest_pipeline_run_strategy'):
    base_run_id = None
    logging.info('Using latest_pipeline_run_strategy.')
  else:
    raise ValueError('artifact_reuse_strategy not set in ChiefSettings.')
  with metadata.Metadata(connection_config=mlmd_connection_config) as m:
    logging.info('Preparing to reuse artifacts.')
    reuse_pipeline_run_artifacts(m, pipeline, base_run_id=base_run_id)
    logging.info('Artifact reuse complete.')


def reuse_node_outputs(metadata_handler: metadata.Metadata, pipeline_name: str,
                       node_id: str, base_run_id: str, new_run_id: str):
  """Reuses the output Artifacts of a pipeline node from a previous pipeline run.

  This copies the latest successful execution associated with the pipeline,
  the old pipeline run id, and node_id, and publishes it as a new cache
  execution, but associated with the new pipeline run id. This makes the output
  artifacts from that execution available for the new pipeline run, which is
  necessary to make partial run work.

  Args:
    metadata_handler: A handler to access MLMD store.
    pipeline_name: The name of the pipeline.
    node_id: The node id.
    base_run_id: The pipeline_run_id where the output artifacts were produced.
    new_run_id: The pipeline_run_id to make the output artifacts available in.
  """
  artifact_recycler = _ArtifactRecycler(metadata_handler, pipeline_name,
                                        new_run_id)
  artifact_recycler.reuse_node_outputs(node_id, base_run_id)


def _get_validated_new_run_id(pipeline: pipeline_pb2.Pipeline,
                              new_run_id: Optional[str] = None) -> str:
  """Attempts to obtain a unique new_run_id.

  Args:
    pipeline: The pipeline IR, whose runtime parameters are already resolved.
    new_run_id: The pipeline_run_id to associate those output artifacts with.
      This function will always attempt to infer the new run id from `pipeline`.
      If not found, it would use the provided `new_run_id`. If found, and
      `new_run_id` is provided, it would verify that it equals the inferred run
      id, and raise an error if they are not equal.

  Returns:
    The validated pipeline_run_id.

  Raises:
    ValueError: If `pipeline` does not contain a pipeline run id, and
      `new_run_id` is not provided.
    ValueError: If `pipeline` does contain a pipeline run id, and
      `new_run_id` is provided, but they are not equal.
  """
  inferred_new_run_id = None
  run_id_value = pipeline.runtime_spec.pipeline_run_id
  if run_id_value.HasField('field_value'):
    inferred_new_run_id = run_id_value.field_value.string_value

  if not (inferred_new_run_id or new_run_id):
    raise ValueError(
        'Unable to infer new pipeline run id. Either resolve the '
        'pipeline_run_id RuntimeParameter in `filtered_pipeline` first, or '
        'provide a `new_run_id` explicitly.')

  if new_run_id and inferred_new_run_id and new_run_id != inferred_new_run_id:
    raise ValueError(
        'Conflicting new pipeline run ids found. pipeline_run_id='
        f'{inferred_new_run_id} was inferred from `full_pipeline`, while '
        f'new_run_id={new_run_id} was explicitly provided. '
        'Consider omitting `new_run_id`, and simply use the pipeline_run_id '
        'inferred from `full_pipeline` as the new_run_id.')

  # The following OR expression will never evaluate to None, because we have
  # already checked above. However, pytype doesn't know that, so we need to cast
  # to the expression to str so that the return type is str.
  return str(inferred_new_run_id or new_run_id)


def _compute_nodes_to_reuse(marked_pipeline: pipeline_pb2.Pipeline) -> Set[str]:
  """Computes which nodes' outputs to reuse.

  Args:
    marked_pipeline: The output of the mark_pipeline function.

  Returns:
    The set of node_ids corresponding to the nodes whose outputs are to be
    reused.

  Raises:
    ValueError: If the filtered_nodes are such that if they are the only nodes
      that are run in a partial run, will inevitably lead to an inconsistent
      MLMD state. Most likely, this means that the user did not directly use the
      outputs of `mark_pipeline` as the inputs to this function.
  """
  node_map = _make_ordered_node_map(marked_pipeline)
  nodes_to_run = []
  skipped_nodes_w_included_children = []
  for node in marked_pipeline.nodes:
    node_id = node.pipeline_node.node_info.id
    node_exec_opts = node.pipeline_node.execution_options
    if node_exec_opts.HasField('run'):
      nodes_to_run.append(node_id)
    elif node_exec_opts.HasField('skip'):
      if node_exec_opts.skip.child_in_partial_run:
        skipped_nodes_w_included_children.append(node_id)
  exclusion_set = _traverse(
      node_map, _Direction.DOWNSTREAM, start_nodes=nodes_to_run)
  inclusion_set = _traverse(
      node_map,
      _Direction.UPSTREAM,
      start_nodes=skipped_nodes_w_included_children)
  if not exclusion_set.isdisjoint(inclusion_set):
    raise ValueError('This should never happen. '
                     'Did you modify the outputs of filter_pipeline?')
  # This is the maximal set of node executions that can be reused.
  return set(node_map.keys()) - exclusion_set


def reuse_pipeline_run_artifacts(metadata_handler: metadata.Metadata,
                                 marked_pipeline: pipeline_pb2.Pipeline,
                                 base_run_id: Optional[str] = None,
                                 new_run_id: Optional[str] = None):
  """Reuses the output Artifacts from a previous pipeline run.

  This computes the maximal set of nodes whose outputs can be associated with
  the new pipeline_run without creating any inconsistencies, and reuses their
  node outputs (similar to repeatedly calling `reuse_node_outputs`). It also
  puts a ParentContext into MLMD, with the `base_run_id` being the parent
  context, and the new run_id (provided by the user, or inferred from
  `pipeline`) as the child context.

  Args:
    metadata_handler: A handler to access MLMD store.
    marked_pipeline: The output of mark_pipeline function.
    base_run_id: The pipeline_run_id where the output artifacts were produced.
      Defaults to the latest previous pipeline run to use as base_run_id.
    new_run_id: The pipeline_run_id to associate those output artifacts with.
      This function will always attempt to infer the new run id from
      `full_pipeline`'s IR. If not found, it would use the provided
      `new_run_id`. If found, and `new_run_id` is provided, it would verify that
      it is the same as the inferred run id, and raise an error if they are not
      the same.

  Raises:
    ValueError: If `full_pipeline` does not contain a pipeline run id, and
      `new_run_id` is not provided.
    ValueError: If `full_pipeline` does contain a pipeline run id, and
      `new_run_id` is provided, but they are not the same.
    ValueError: If the filtered_nodes are such that if they are the only nodes
      that are run in a partial run, will inevitably lead to an inconsistent
      MLMD state. Most likely, this means that the user did not directly use the
      outputs of `filter_pipeline` as the inputs to this function.
  """
  validated_new_run_id = _get_validated_new_run_id(marked_pipeline, new_run_id)
  nodes_to_reuse = _compute_nodes_to_reuse(marked_pipeline)
  artifact_recycler = _ArtifactRecycler(
      metadata_handler,
      pipeline_name=marked_pipeline.pipeline_info.id,
      new_run_id=validated_new_run_id)
  if not base_run_id:
    base_run_id = artifact_recycler.get_latest_pipeline_run_id()
    logging.info(
        'base_run_id not provided. '
        'Default to latest pipeline run: %s', base_run_id)
  for node_id in nodes_to_reuse:
    artifact_recycler.reuse_node_outputs(node_id, base_run_id)
  artifact_recycler.put_parent_context(base_run_id)


class _ArtifactRecycler:
  """Allows previously-generated Artifacts to be used in a new pipeline run.

  By implementing this in a class (instead of a function), we reduce the
  number of MLMD reads when reusing the outputs of multiple nodes in the same
  pipeline run.
  """

  def __init__(self, metadata_handler: metadata.Metadata, pipeline_name: str,
               new_run_id: str):
    self._mlmd = metadata_handler
    self._pipeline_name = pipeline_name
    self._pipeline_context = self._get_pipeline_context()
    self._new_run_id = new_run_id
    self._pipeline_run_type_id = self._mlmd.store.get_context_type(
        constants.PIPELINE_RUN_CONTEXT_TYPE_NAME).id
    # Query and store all pipeline run contexts. This has multiple advantages:
    # - No need to worry about other pipeline runs that may be taking place
    #   concurrently and changing MLMD state.
    # - Fewer MLMD queries.
    # TODO(b/196981304): Ensure there are no pipeline runs from other pipelines.
    self._pipeline_run_contexts = {
        run_ctx.name: run_ctx
        for run_ctx in self._mlmd.store.get_contexts_by_type(
            constants.PIPELINE_RUN_CONTEXT_TYPE_NAME)
    }

  def _get_pipeline_context(self) -> metadata_store_pb2.Context:
    result = self._mlmd.store.get_context_by_type_and_name(
        type_name=constants.PIPELINE_CONTEXT_TYPE_NAME,
        context_name=self._pipeline_name)
    if result is None:
      raise LookupError(f'pipeline {self._pipeline_name} not found in MLMD.')
    return result

  def _get_pipeline_run_context(
      self,
      run_id: str,
      register_if_not_found: bool = False) -> metadata_store_pb2.Context:
    """Gets the pipeline_run_context for a given pipeline run id.

    When called, it will first attempt to get the pipeline run context from the
    in-memory cache. If not found there, it will raise LookupError unless
    `register_if_not_found` is set to True. If `register_if_not_found` is set to
    True, this method will register the pipeline_run_context in MLMD, add it to
    the in-memory cache, and return the pipeline_run_context.

    Args:
      run_id: The pipeline_run_id whose Context to query.
      register_if_not_found: If set to True, it will register the
        pipeline_run_id in MLMD if the pipeline_run_id cannot be found in MLMD.
        If set to False, it will raise LookupError.  Defaults to False.

    Returns:
      The requested pipeline run Context.

    Raises:
      LookupError: If register_if_not_found is not set to True, and the
        pipeline_run_id cannot be found in MLMD.
    """
    if run_id not in self._pipeline_run_contexts:
      if register_if_not_found:
        pipeline_run_context = context_lib.register_context_if_not_exists(
            self._mlmd,
            context_type_name=constants.PIPELINE_RUN_CONTEXT_TYPE_NAME,
            context_name=run_id)
        self._pipeline_run_contexts[run_id] = pipeline_run_context
      else:
        raise LookupError(f'pipeline_run_id {run_id} not found in MLMD.')
    return self._pipeline_run_contexts[run_id]

  def get_latest_pipeline_run_id(self) -> str:
    """Gets the latest previous pipeline_run_id."""
    latest_previous_run_ctx = None  # type: metadata_store_pb2.Context
    for pipeline_run_context in self._pipeline_run_contexts.values():
      if pipeline_run_context.name == self._new_run_id:
        continue
      if not latest_previous_run_ctx:
        latest_previous_run_ctx = pipeline_run_context
        continue
      if (pipeline_run_context.create_time_since_epoch >
          latest_previous_run_ctx.create_time_since_epoch):
        latest_previous_run_ctx = pipeline_run_context
    if not latest_previous_run_ctx:
      raise LookupError(
          'No previous pipeline_run_ids found. '
          'You need to have completed a pipeline run before performing a '
          'partial run with artifact reuse.')
    return latest_previous_run_ctx.name

  def _get_node_context(self, node_id: str) -> metadata_store_pb2.Context:
    node_context_name = compiler_utils.node_context_name(
        self._pipeline_name, node_id)
    result = self._mlmd.store.get_context_by_type_and_name(
        type_name=constants.NODE_CONTEXT_TYPE_NAME,
        context_name=node_context_name)
    if result is None:
      raise LookupError(f'node context {node_context_name} not found in MLMD.')
    return result

  def _get_successful_executions(
      self, node_id: str, run_id: str) -> List[metadata_store_pb2.Execution]:
    """Gets all successful Executions of a given node in a given pipeline run.

    Args:
      node_id: The node whose Executions to query.
      run_id: The pipeline run id to query the Executions from.

    Returns:
      All successful executions for that node at that run_id.

    Raises:
      LookupError: If no successful Execution was found.
    """
    node_context = self._get_node_context(node_id)
    base_run_context = self._get_pipeline_run_context(run_id)
    all_associated_executions = (
        execution_lib.get_executions_associated_with_all_contexts(
            self._mlmd,
            contexts=[node_context, base_run_context, self._pipeline_context]))
    prev_successful_executions = [
        e for e in all_associated_executions
        if execution_lib.is_execution_successful(e)
    ]
    if not prev_successful_executions:
      raise LookupError(
          f'No previous successful executions found for node_id {node_id} in '
          f'pipeline_run {run_id}')

    return execution_lib.sort_executions_newest_to_oldest(
        prev_successful_executions)

  def _get_cached_execution_contexts(
      self,
      existing_execution: metadata_store_pb2.Execution,
  ) -> List[metadata_store_pb2.Context]:
    """Gets the list of Contexts to be associated with the new cached Execution.

    Copies all the Contexts associated with the existing execution, except for
    the pipeline run context, which is updated with new pipeline run id.

    Args:
      existing_execution: The existing execution to copy from.

    Returns:
      The list of Contexts to be associated with the new cached Execution.
    """
    result = []
    for context in self._mlmd.store.get_contexts_by_execution(
        existing_execution.id):
      if context.type_id == self._pipeline_run_type_id:
        # Replace with new pipeline run context.
        context = self._get_pipeline_run_context(
            self._new_run_id, register_if_not_found=True)
      result.append(context)
    return result

  def _cache_and_publish(self,
                         existing_execution: metadata_store_pb2.Execution):
    """Updates MLMD."""
    cached_execution_contexts = self._get_cached_execution_contexts(
        existing_execution)
    # Check if there are any previous attempts to cache and publish.
    prev_cache_executions = (
        execution_lib.get_executions_associated_with_all_contexts(
            self._mlmd, contexts=cached_execution_contexts))
    if not prev_cache_executions:
      new_execution = execution_publish_utils.register_execution(
          self._mlmd,
          execution_type=metadata_store_pb2.ExecutionType(
              id=existing_execution.type_id),
          contexts=cached_execution_contexts)
    else:
      if len(prev_cache_executions) > 1:
        logging.warning(
            'More than one previous cache executions seen when attempting '
            'reuse_node_outputs: %s', prev_cache_executions)

      if (prev_cache_executions[-1].last_known_state ==
          metadata_store_pb2.Execution.CACHED):
        return
      else:
        new_execution = prev_cache_executions[-1]

    output_artifacts = execution_lib.get_artifacts_dict(
        self._mlmd,
        existing_execution.id,
        event_type=metadata_store_pb2.Event.OUTPUT)

    execution_publish_utils.publish_cached_execution(
        self._mlmd,
        contexts=cached_execution_contexts,
        execution_id=new_execution.id,
        output_artifacts=output_artifacts)

  def put_parent_context(self, base_run_id: str):
    """Puts a ParentContext edge in MLMD.

    Args:
      base_run_id: The new pipeline_run_id to be set as the parent context. The
        child context is the new pipeline_run_id that this _ArtifactRecycler
        instance was created with.
    """
    base_run_context = self._get_pipeline_run_context(base_run_id)
    new_run_context = self._get_pipeline_run_context(
        self._new_run_id, register_if_not_found=True)
    context_lib.put_parent_context_if_not_exists(
        self._mlmd, parent_id=base_run_context.id, child_id=new_run_context.id)

  def reuse_node_outputs(self, node_id: str, base_run_id: str):
    """Makes the outputs of `node_id` available to new_pipeline_run_id."""
    previous_executions = self._get_successful_executions(node_id, base_run_id)
    for previous_execution in previous_executions:
      self._cache_and_publish(previous_execution)
