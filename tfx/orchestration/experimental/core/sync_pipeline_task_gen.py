# Copyright 2020 Google LLC. All Rights Reserved.
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
"""TaskGenerator implementation for sync pipelines."""

from typing import Callable, Hashable, List, Optional, Sequence, Set

from absl import logging
import cachetools
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import constants
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import service_jobs
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.experimental.core import task_gen
from tfx.orchestration.experimental.core import task_gen_utils
from tfx.orchestration.portable import cache_utils
from tfx.orchestration.portable import execution_publish_utils
from tfx.orchestration.portable import outputs_utils
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import pipeline_pb2
from tfx.utils import status as status_lib
from tfx.utils import topsort

from google.protobuf import any_pb2
from ml_metadata.proto import metadata_store_pb2

# Caches successful and skipped nodes so we don't have to query MLMD repeatedly.
_successful_nodes_cache = cachetools.LRUCache(maxsize=1024)


class SyncPipelineTaskGenerator(task_gen.TaskGenerator):
  """Task generator for executing a sync pipeline.

  Calling `generate` is not thread-safe. Concurrent calls to `generate` should
  be explicitly serialized. Since MLMD may be updated upon call to `generate`,
  it's also not safe to call `generate` on different instances of this class
  where the instances refer to the same MLMD db and the same pipeline IR.
  """

  def __init__(self, mlmd_handle: metadata.Metadata,
               pipeline_state: pstate.PipelineState,
               is_task_id_tracked_fn: Callable[[task_lib.TaskId], bool],
               service_job_manager: service_jobs.ServiceJobManager):
    """Constructs `SyncPipelineTaskGenerator`.

    Args:
      mlmd_handle: A handle to the MLMD db.
      pipeline_state: Pipeline state.
      is_task_id_tracked_fn: A callable that returns `True` if a task_id is
        tracked by the task queue.
      service_job_manager: Used for handling service nodes in the pipeline.
    """
    self._mlmd_handle = mlmd_handle
    pipeline = pipeline_state.pipeline
    if pipeline.execution_mode != pipeline_pb2.Pipeline.ExecutionMode.SYNC:
      raise ValueError(
          'SyncPipelineTaskGenerator should be instantiated with a pipeline '
          'proto having execution_mode `SYNC`, not `{}`'.format(
              pipeline.execution_mode))
    for node in pipeline.nodes:
      which_node = node.WhichOneof('node')
      if which_node != 'pipeline_node':
        raise ValueError(
            'All sync pipeline nodes should be of type `PipelineNode`; found: '
            '`{}`'.format(which_node))
    self._pipeline_state = pipeline_state
    self._pipeline_uid = self._pipeline_state.pipeline_uid
    self._pipeline = pipeline
    self._pipeline_run_id = (
        pipeline.runtime_spec.pipeline_run_id.field_value.string_value)
    self._is_task_id_tracked_fn = is_task_id_tracked_fn
    self._service_job_manager = service_job_manager

  def generate(self) -> List[task_lib.Task]:
    """Generates tasks for executing the next executable nodes in the pipeline.

    The returned tasks must have `exec_task` populated. List may be empty if
    no nodes are ready for execution.

    Returns:
      A `list` of tasks to execute.
    """
    layers = _topsorted_layers(self._pipeline)
    terminal_node_ids = _terminal_node_ids(layers)
    exec_node_tasks = []
    update_node_state_tasks = []
    successful_node_ids = set()
    finalize_pipeline_task = None
    for layer_nodes in layers:
      for node in layer_nodes:
        tasks = self._generate_tasks_for_node(node, successful_node_ids)
        for task in tasks:
          if task_lib.is_update_node_state_task(task):
            update_node_state_tasks.append(task)
          elif task_lib.is_exec_node_task(task):
            exec_node_tasks.append(task)
          else:
            assert task_lib.is_finalize_pipeline_task(task)
            finalize_pipeline_task = task

        if finalize_pipeline_task:
          break

      if finalize_pipeline_task:
        break

      layer_node_ids = set(node.node_info.id for node in layer_nodes)
      successful_layer_node_ids = layer_node_ids & successful_node_ids
      self._update_successful_nodes_cache(successful_layer_node_ids)

    result = update_node_state_tasks
    if finalize_pipeline_task:
      result.append(finalize_pipeline_task)
    elif terminal_node_ids <= successful_node_ids:
      # If all terminal nodes are successful, the pipeline can be finalized.
      result.append(
          task_lib.FinalizePipelineTask(
              pipeline_uid=self._pipeline_uid,
              status=status_lib.Status(code=status_lib.Code.OK)))
    else:
      result.extend(exec_node_tasks)
    return result

  def _generate_tasks_for_node(
      self, node: pipeline_pb2.PipelineNode,
      successful_node_ids: Set[str]) -> List[task_lib.Task]:
    """Generates list of tasks for the given node."""
    node_uid = task_lib.NodeUid.from_pipeline_node(self._pipeline, node)
    node_id = node.node_info.id
    result = []

    if self._in_successful_nodes_cache(node_uid):
      successful_node_ids.add(node_id)
      return result

    if not self._upstream_nodes_successful(node, successful_node_ids):
      return result

    with self._pipeline_state:
      node_state = self._pipeline_state.get_node_state(node_uid)
      if node_state.state in (pstate.NodeState.STOPPING,
                              pstate.NodeState.STOPPED):
        logging.info('Ignoring node in state \'%s\' for task generation: %s',
                     node_state.state, node_uid)
        return result

    # If this is a pure service node, there is no ExecNodeTask to generate
    # but we ensure node services and check service status.
    service_status = self._ensure_node_services_if_pure(node_id)
    if service_status is not None:
      if service_status == service_jobs.ServiceStatus.FAILED:
        error_msg = f'service job failed; node uid: {node_uid}'
        result.append(
            task_lib.UpdateNodeStateTask(
                node_uid=node_uid,
                state=pstate.NodeState.FAILED,
                status=status_lib.Status(
                    code=status_lib.Code.ABORTED, message=error_msg)))
        result.append(self._abort_task(error_msg))
      elif service_status == service_jobs.ServiceStatus.SUCCESS:
        logging.info('Service node successful: %s', node_uid)
        result.append(
            task_lib.UpdateNodeStateTask(
                node_uid=node_uid, state=pstate.NodeState.COMPLETE))
        successful_node_ids.add(node_id)
      elif service_status == service_jobs.ServiceStatus.RUNNING:
        result.append(
            task_lib.UpdateNodeStateTask(
                node_uid=node_uid, state=pstate.NodeState.RUNNING))
      return result

    # If a task for the node is already tracked by the task queue, it need
    # not be considered for generation again but we ensure node services
    # in case of a mixed service node.
    if self._is_task_id_tracked_fn(
        task_lib.exec_node_task_id_from_pipeline_node(self._pipeline, node)):
      service_status = self._ensure_node_services_if_mixed(node_id)
      if service_status == service_jobs.ServiceStatus.FAILED:
        error_msg = f'associated service job failed; node uid: {node_uid}'
        result.append(
            task_lib.UpdateNodeStateTask(
                node_uid=node_uid,
                state=pstate.NodeState.FAILED,
                status=status_lib.Status(
                    code=status_lib.Code.ABORTED, message=error_msg)))
        result.append(self._abort_task(error_msg))
      return result

    node_executions = task_gen_utils.get_executions(self._mlmd_handle, node)
    latest_execution = task_gen_utils.get_latest_execution(node_executions)

    # If the latest execution is successful, we're done.
    if latest_execution and execution_lib.is_execution_successful(
        latest_execution):
      logging.info('Node successful: %s', node_uid)
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid, state=pstate.NodeState.COMPLETE))
      successful_node_ids.add(node_id)
      return result

    # If the latest execution failed or cancelled, the pipeline should be
    # aborted if the node is not in state STARTING. For nodes that are
    # in state STARTING, a new execution is created.
    if (latest_execution and
        not execution_lib.is_execution_active(latest_execution) and
        node_state.state != pstate.NodeState.STARTING):
      error_msg_value = latest_execution.custom_properties.get(
          constants.EXECUTION_ERROR_MSG_KEY)
      error_msg = data_types_utils.get_metadata_value(
          error_msg_value) if error_msg_value else ''
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid,
              state=pstate.NodeState.FAILED,
              status=status_lib.Status(
                  code=status_lib.Code.ABORTED, message=error_msg)))
      result.append(
          self._abort_task(
              f'node failed; node uid: {node_uid}; error: {error_msg}'))
      return result

    exec_node_task = task_gen_utils.generate_task_from_active_execution(
        self._mlmd_handle, self._pipeline, node, node_executions)
    if exec_node_task:
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid, state=pstate.NodeState.RUNNING))
      result.append(exec_node_task)
      return result

    # Finally, we are ready to generate tasks for the node by resolving inputs.
    result.extend(
        self._resolve_inputs_and_generate_tasks_for_node(
            node, node_executions, successful_node_ids))
    return result

  def _resolve_inputs_and_generate_tasks_for_node(
      self, node: pipeline_pb2.PipelineNode,
      node_executions: Sequence[metadata_store_pb2.Execution],
      successful_node_ids: Set[str]) -> List[task_lib.Task]:
    """Generates tasks for a node by freshly resolving inputs."""
    result = []
    node_uid = task_lib.NodeUid.from_pipeline_node(self._pipeline, node)
    resolved_info = task_gen_utils.generate_resolved_info(
        self._mlmd_handle, node)
    if resolved_info is None:
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid,
              state=pstate.NodeState.SKIPPED))
      successful_node_ids.add(node.node_info.id)
      return result
    if resolved_info.input_artifacts is None:
      error_msg = f'failure to resolve inputs; node uid: {node_uid}'
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid,
              state=pstate.NodeState.FAILED,
              status=status_lib.Status(
                  code=status_lib.Code.ABORTED, message=error_msg)))
      result.append(self._abort_task(error_msg))
      return result

    execution = execution_publish_utils.register_execution(
        metadata_handler=self._mlmd_handle,
        execution_type=node.node_info.type,
        contexts=resolved_info.contexts,
        input_artifacts=resolved_info.input_artifacts,
        exec_properties=resolved_info.exec_properties)
    outputs_resolver = outputs_utils.OutputsResolver(
        node, self._pipeline.pipeline_info, self._pipeline.runtime_spec,
        self._pipeline.execution_mode)
    output_artifacts = outputs_resolver.generate_output_artifacts(execution.id)

    # Check if we can elide node execution by reusing previously computed
    # outputs for the node.
    cache_context = cache_utils.get_cache_context(
        self._mlmd_handle,
        pipeline_node=node,
        pipeline_info=self._pipeline.pipeline_info,
        executor_spec=_get_executor_spec(self._pipeline, node.node_info.id),
        input_artifacts=resolved_info.input_artifacts,
        output_artifacts=output_artifacts,
        parameters=resolved_info.exec_properties)
    contexts = resolved_info.contexts + [cache_context]
    if node.execution_options.caching_options.enable_cache:
      cached_outputs = cache_utils.get_cached_outputs(
          self._mlmd_handle, cache_context=cache_context)
      if cached_outputs is not None:
        logging.info(
            'Eliding node execution, using cached outputs; node uid: %s',
            node_uid)
        execution_publish_utils.publish_cached_execution(
            self._mlmd_handle,
            contexts=contexts,
            execution_id=execution.id,
            output_artifacts=cached_outputs)
        successful_node_ids.add(node.node_info.id)
        pstate.record_state_change_time()
        result.append(
            task_lib.UpdateNodeStateTask(
                node_uid=node_uid, state=pstate.NodeState.COMPLETE))
        return result

    # For mixed service nodes, we ensure node services and check service
    # status; pipeline is aborted if the service jobs have failed.
    service_status = self._ensure_node_services_if_mixed(node.node_info.id)
    if service_status == service_jobs.ServiceStatus.FAILED:
      error_msg = f'associated service job failed; node uid: {node_uid}'
      result.append(
          task_lib.UpdateNodeStateTask(
              node_uid=node_uid,
              state=pstate.NodeState.FAILED,
              status=status_lib.Status(
                  code=status_lib.Code.ABORTED, message=error_msg)))
      result.append(self._abort_task(error_msg))
      return result

    outputs_utils.make_output_dirs(output_artifacts)
    result.append(
        task_lib.UpdateNodeStateTask(
            node_uid=node_uid, state=pstate.NodeState.RUNNING))
    result.append(
        task_lib.ExecNodeTask(
            node_uid=node_uid,
            execution_id=execution.id,
            contexts=contexts,
            input_artifacts=resolved_info.input_artifacts,
            exec_properties=resolved_info.exec_properties,
            output_artifacts=output_artifacts,
            executor_output_uri=outputs_resolver.get_executor_output_uri(
                execution.id),
            stateful_working_dir=outputs_resolver
            .get_stateful_working_directory(execution.id),
            pipeline=self._pipeline))
    return result

  def _ensure_node_services_if_pure(
      self, node_id: str) -> Optional[service_jobs.ServiceStatus]:
    """Calls `ensure_node_services` and returns status if given node is pure service node."""
    if self._service_job_manager.is_pure_service_node(self._pipeline_state,
                                                      node_id):
      return self._service_job_manager.ensure_node_services(
          self._pipeline_state, node_id)
    return None

  def _ensure_node_services_if_mixed(
      self, node_id: str) -> Optional[service_jobs.ServiceStatus]:
    """Calls `ensure_node_services` and returns status if given node is mixed service node."""
    if self._service_job_manager.is_mixed_service_node(self._pipeline_state,
                                                       node_id):
      return self._service_job_manager.ensure_node_services(
          self._pipeline_state, node_id)
    return None

  def _upstream_nodes_successful(self, node: pipeline_pb2.PipelineNode,
                                 successful_node_ids: Set[str]) -> bool:
    """Returns `True` if all the upstream nodes have been successfully executed."""
    return set(node.upstream_nodes) <= successful_node_ids

  def _abort_task(self, error_msg: str) -> task_lib.FinalizePipelineTask:
    """Returns task to abort pipeline execution."""
    error_msg = (f'Aborting pipeline execution due to node execution failure; '
                 f'error: {error_msg}')
    logging.error(error_msg)
    return task_lib.FinalizePipelineTask(
        pipeline_uid=self._pipeline_uid,
        status=status_lib.Status(
            code=status_lib.Code.ABORTED, message=error_msg))

  def _update_successful_nodes_cache(self, node_ids: Set[str]) -> None:
    for node_id in node_ids:
      node_uid = task_lib.NodeUid(
          pipeline_uid=self._pipeline_uid, node_id=node_id)
      _successful_nodes_cache[self._node_cache_key(node_uid)] = True

  def _in_successful_nodes_cache(self, node_uid) -> bool:
    return _successful_nodes_cache.get(self._node_cache_key(node_uid), False)

  def _node_cache_key(self, node_uid: task_lib.NodeUid) -> Hashable:
    return (self._pipeline_run_id, node_uid)


# TODO(b/182944474): Raise error in _get_executor_spec if executor spec is
# missing for a non-system node.
def _get_executor_spec(pipeline: pipeline_pb2.Pipeline,
                       node_id: str) -> Optional[any_pb2.Any]:
  """Returns executor spec for given node_id if it exists in pipeline IR, None otherwise."""
  if not pipeline.deployment_config.Is(
      pipeline_pb2.IntermediateDeploymentConfig.DESCRIPTOR):
    return None
  depl_config = pipeline_pb2.IntermediateDeploymentConfig()
  pipeline.deployment_config.Unpack(depl_config)
  return depl_config.executor_specs.get(node_id)


def _topsorted_layers(
    pipeline: pipeline_pb2.Pipeline) -> List[List[pipeline_pb2.PipelineNode]]:
  """Returns pipeline nodes in topologically sorted layers."""
  node_by_id = {
      node.pipeline_node.node_info.id: node.pipeline_node
      for node in pipeline.nodes
  }
  return topsort.topsorted_layers(
      [node.pipeline_node for node in pipeline.nodes],
      get_node_id_fn=lambda node: node.node_info.id,
      get_parent_nodes=(
          lambda node: [node_by_id[n] for n in node.upstream_nodes]),
      get_child_nodes=(
          lambda node: [node_by_id[n] for n in node.downstream_nodes]))


def _terminal_node_ids(
    layers: List[List[pipeline_pb2.PipelineNode]]) -> Set[str]:
  """Returns nodes across all layers that have no downstream nodes."""
  terminal_node_ids: Set[str] = set()
  for layer_nodes in layers:
    for node in layer_nodes:
      if not node.downstream_nodes:
        terminal_node_ids.add(node.node_info.id)
  return terminal_node_ids
