# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Api client adapter containers commands."""
import httplib
from os import linesep
import time

from apitools.base.py import exceptions as apitools_exceptions
from apitools.base.py import http_wrapper

from googlecloudsdk.api_lib.compute import constants
from googlecloudsdk.api_lib.container import util
from googlecloudsdk.api_lib.util import apis as core_apis
from googlecloudsdk.calliope import exceptions
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core import resources as cloud_resources
from googlecloudsdk.core.console import progress_tracker

WRONG_ZONE_ERROR_MSG = """\
{error}
Could not find [{name}] in [{wrong_zone}].
Did you mean [{name}] in [{zone}]?"""

NO_SUCH_CLUSTER_ERROR_MSG = """\
{error}
No cluster named '{name}' in {project}."""

NO_SUCH_NODE_POOL_ERROR_MSG = """\
No node pool named '{name}' in {cluster}."""

NO_NODE_POOL_SELECTED_ERROR_MSG = """\
Please specify one of the following node pools:
"""

MISMATCH_AUTHORIZED_NETWORKS_ERROR_MSG = """\
Cannot use --main-authorized-networks \
if --enable-main-authorized-networks is not \
specified."""

NO_AUTOPROVISIONING_MSG = """\
Node autoprovisioning is currently in alpha. Please contact GKE support \
if you're interested in enabling alpha features in your cluster.
"""

NO_AUTOPROVISIONING_LIMITS_ERROR_MSG = """\
Must specify both --max-cpu and --max-memory to enable autoprovisioning.
"""

MISMATCH_ACCELERATOR_TYPE_LIMITS_ERROR_MSG = """\
Maximum and minimum accelerator limits must be set on the same accelerator type.
"""

NO_SUCH_LABEL_ERROR_MSG = """\
No label named '{name}' found on cluster '{cluster}'."""

NO_LABELS_ON_CLUSTER_ERROR_MSG = """\
Cluster '{cluster}' has no labels to remove."""

CREATE_SUBNETWORK_INVALID_KEY_ERROR_MSG = """\
Invalid key '{key}' for --create-subnetwork (must be one of 'name', 'range').
"""

CREATE_SUBNETWORK_WITH_SUBNETWORK_ERROR_MSG = """\
Cannot specify both --subnetwork and --create-subnetwork at the same time.
"""

CREATE_SUBNETWORK_WITH_EXPLICIT_SECONDARY_RANGES_ERROR_MSG = """\
Cannot use --create-subnetwork with explicit secondary range options
(--cluster-secondary-range-name, --services-secondary-range-name).
"""

MISSING_EXPLICIT_SECONDARY_RANGE_ERROR_MSG = """\
Must specify both --cluster-secondary-range-name and --services-secondary-range-name.
"""

NODE_TAINT_INCORRECT_FORMAT_ERROR_MSG = """\
Invalid value [{key}={value}] for argument --node-taints. Node taint is of format key=value:effect
"""

NODE_TAINT_INCORRECT_EFFECT_ERROR_MSG = """\
Invalid taint effect [{effect}] for argument --node-taints. Valid effect values are NoSchedule, PreferNoSchedule, NoExecute'
"""

LOCAL_SSD_INCORRECT_FORMAT_ERROR_MSG = """\
Invalid local SSD format [{err_format}] for argument --local-ssd-volumes. Valid formats are fs, block
"""

UNKNOWN_WORKLOAD_METADATA_FROM_NODE_ERROR_MSG = """\
Invalid option '{option}' for '--workload-metadata-from-node' (must be one of 'unspecified', 'secure', 'exposed').
"""

ALLOW_ROUTE_OVERLAP_WITHOUT_CLUSTER_CIDR_ERROR_MSG = """\
Flag --cluster-ipv4-cidr must be fully specified (e.g. `10.96.0.0/14`, but not `/14`) with --allow-route-overlap.
"""

ALLOW_ROUTE_OVERLAP_WITHOUT_SERVICES_CIDR_ERROR_MSG = """\
Flag --services-ipv4-cidr must be fully specified (e.g. `10.96.0.0/14`, but not `/14`) with --allow-route-overlap and --enable-ip-alias.
"""

PREREQUISITE_OPTION_ERROR_MSG = """\
Cannot specify --{opt} without --{prerequisite}.
"""

MAX_NODES_PER_POOL = 1000

INGRESS = 'HttpLoadBalancing'
HPA = 'HorizontalPodAutoscaling'
DASHBOARD = 'KubernetesDashboard'
DEFAULT_ADDONS = [INGRESS, HPA]
ADDONS_OPTIONS = [INGRESS, HPA, DASHBOARD]
NETWORK_POLICY = 'NetworkPolicy'

UNSPECIFIED = 'UNSPECIFIED'
SECURE = 'SECURE'
EXPOSE = 'EXPOSE'


def CheckResponse(response):
  """Wrap http_wrapper.CheckResponse to skip retry on 503."""
  if response.status_code == 503:
    raise apitools_exceptions.HttpError.FromResponse(response)
  return http_wrapper.CheckResponse(response)


def NewAPIAdapter(api_version):
  if api_version is 'v1alpha1':
    return NewV1Alpha1APIAdapter()
  elif api_version is 'v1beta1':
    return NewV1Beta1APIAdapter()
  else:
    return NewV1APIAdapter()


def NewV1APIAdapter():
  return InitAPIAdapter('v1', V1Adapter)


def NewV1Beta1APIAdapter():
  return InitAPIAdapter('v1beta1', V1Beta1Adapter)


def NewV1Alpha1APIAdapter():
  return InitAPIAdapter('v1alpha1', V1Alpha1Adapter)


def InitAPIAdapter(api_version, adapter):
  """Initialize an api adapter.

  Args:
    api_version: the api version we want.
    adapter: the api adapter constructor.
  Returns:
    APIAdapter object.
  """

  api_client = core_apis.GetClientInstance('container', api_version)
  api_client.check_response_func = CheckResponse
  messages = core_apis.GetMessagesModule('container', api_version)

  registry = cloud_resources.REGISTRY.Clone()
  registry.RegisterApiByName('container', api_version)
  registry.RegisterApiByName('compute', 'v1')

  return adapter(registry, api_client, messages)


_SERVICE_ACCOUNT_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)

_OLD_REQUIRED_SCOPES = (
    'https://www.googleapis.com/auth/compute',
    'https://www.googleapis.com/auth/devstorage.read_only')

_ENDPOINTS_SCOPES = (
    'https://www.googleapis.com/auth/servicecontrol',
    'https://www.googleapis.com/auth/service.management.readonly')


def NodeIdentityOptionsToNodeConfig(options, node_config):
  """Convert node identity options into node config.

  If a service account was provided, use that and cloud-platform scope.
  Otherwise, if options.new_scopes_behavior is True (we're in alpha or beta
  track), or container/new_scopes_behavior property is set, just use what's
  passed to --scopes (or the default).  Otherwise, emulate the deprecated
  behavior: expand the scopes, add or remove endpoints scopes as necessary, add
  compute-rw and devstorage-ro, sort, and return.  Print warnings as necessary.

  Args:
    options: the CreateCluster or CreateNodePool options.
    node_config: the messages.node_config object to be populated.
  """
  if properties.VALUES.container.new_scopes_behavior.GetBool():
    options.new_scopes_behavior = True
  if options.service_account:
    node_config.serviceAccount = options.service_account
    options.scopes = _SERVICE_ACCOUNT_SCOPES
  elif options.new_scopes_behavior:
    options.scopes = ExpandScopeURIs(options.scopes)
  else:
    if not options.scopes:
      options.scopes = []
    # The interactions between --scopes, compute-rw and storage-ro, and
    # --[no-]enable-cloud-endpoints is all kind of whacky behavior.  Expand
    # scope aliases _before_ munging with _OLD_REQUIRED_SCOPES and endpoints
    # scopes so we can avoid adding _OLD_REQUIRED_SCOPES if it's unnecessary,
    # filter out scopes if the --no-enable-cloud-endpoints is set, and print
    # only relevant deprecation warnings.  See b/69554175 and b/69431751.
    options.scopes = ExpandScopeURIs(options.scopes)
    # Add or remove endpoints scopes as necessary.
    if options.enable_cloud_endpoints:
      for scope in _ENDPOINTS_SCOPES:
        if scope not in options.scopes:
          log.warn("""\
The behavior of --scopes will change in a future gcloud release: \
service-control and service-management scopes will no longer be added to what \
is specified in --scopes. To use these scopes, add them explicitly to \
--scopes. To use the new behavior, set container/new_scopes_behavior property \
(gcloud config set container/new_scopes_behavior true).""")
          options.scopes += _ENDPOINTS_SCOPES
          break
    else:
      # Don't print a warning here because the only way to get here is by
      # specifying --no-enable-cloud-endpoints explicitly, which will trigger a
      # deprecation warning regardless.
      options.scopes = [x for x in options.scopes if x not in _ENDPOINTS_SCOPES]
    # Add compute-rw and devstorage-ro as necessary.
    for scope in _OLD_REQUIRED_SCOPES:
      if scope not in options.scopes:
        log.warn("""\
Starting in Kubernetes v1.10, new clusters will no longer get compute-rw and \
storage-ro scopes added to what is specified in --scopes (though the latter \
will remain included in the default --scopes). To use these scopes, add them \
explicitly to --scopes. To use the new behavior, set \
container/new_scopes_behavior property (gcloud config set \
container/new_scopes_behavior true).""")
        options.scopes += _OLD_REQUIRED_SCOPES
        break
  node_config.oauthScopes = sorted(set(options.scopes))


def ExpandScopeURIs(scopes):
  """Expand scope names to the fully qualified uris.

  Args:
    scopes: [str,] list of scope names. Can be short names ('compute-rw') or
    full urls ('https://www.googleapis.com/auth/compute'). See SCOPES in
    api_lib/container/constants.py & api_lib/compute/constants.py.

  Returns:
    list of str, full urls for recognized scopes.
  """

  scope_uris = []
  for scope in scopes:
    # Expand any scope aliases (like 'storage-rw') that the user provided
    # to their official URL representation.
    expanded = constants.SCOPES.get(scope, [scope])
    scope_uris.extend(expanded)
  return scope_uris


class CreateClusterOptions(object):

  def __init__(self,
               node_machine_type=None,
               node_source_image=None,
               node_disk_size_gb=None,
               scopes=None,
               enable_cloud_endpoints=None,
               new_scopes_behavior=None,
               num_nodes=None,
               additional_zones=None,
               node_locations=None,
               user=None,
               password=None,
               cluster_version=None,
               node_version=None,
               network=None,
               cluster_ipv4_cidr=None,
               enable_cloud_logging=None,
               enable_cloud_monitoring=None,
               subnetwork=None,
               addons=None,
               disable_addons=None,
               local_ssd_count=None,
               local_ssd_volume_configs=None,
               tags=None,
               node_labels=None,
               node_taints=None,
               enable_autoscaling=None,
               min_nodes=None,
               max_nodes=None,
               image_type=None,
               max_nodes_per_pool=None,
               enable_kubernetes_alpha=None,
               preemptible=None,
               enable_autorepair=None,
               enable_autoupgrade=None,
               service_account=None,
               enable_main_authorized_networks=None,
               main_authorized_networks=None,
               enable_legacy_authorization=None,
               labels=None,
               disk_type=None,
               enable_network_policy=None,
               services_ipv4_cidr=None,
               enable_ip_alias=None,
               create_subnetwork=None,
               cluster_secondary_range_name=None,
               services_secondary_range_name=None,
               enable_shared_network=None,
               accelerators=None,
               enable_binauthz=None,
               min_cpu_platform=None,
               workload_metadata_from_node=None,
               maintenance_window=None,
               enable_pod_security_policy=None,
               allow_route_overlap=None,
               private_cluster=None,
               main_ipv4_cidr=None,
               tpu_ipv4_cidr=None,
               enable_tpu=None):
    self.node_machine_type = node_machine_type
    self.node_source_image = node_source_image
    self.node_disk_size_gb = node_disk_size_gb
    self.scopes = scopes
    self.enable_cloud_endpoints = enable_cloud_endpoints
    self.new_scopes_behavior = new_scopes_behavior
    self.num_nodes = num_nodes
    self.additional_zones = additional_zones
    self.node_locations = node_locations
    self.user = user
    self.password = password
    self.cluster_version = cluster_version
    self.node_version = node_version
    self.network = network
    self.cluster_ipv4_cidr = cluster_ipv4_cidr
    self.enable_cloud_logging = enable_cloud_logging
    self.enable_cloud_monitoring = enable_cloud_monitoring
    self.subnetwork = subnetwork
    self.disable_addons = disable_addons
    self.addons = addons
    self.local_ssd_count = local_ssd_count
    self.local_ssd_volume_configs = local_ssd_volume_configs
    self.tags = tags
    self.node_labels = node_labels
    self.node_taints = node_taints
    self.enable_autoscaling = enable_autoscaling
    self.min_nodes = min_nodes
    self.max_nodes = max_nodes
    self.image_type = image_type
    self.max_nodes_per_pool = max_nodes_per_pool
    self.enable_kubernetes_alpha = enable_kubernetes_alpha
    self.preemptible = preemptible
    self.enable_autorepair = enable_autorepair
    self.enable_autoupgrade = enable_autoupgrade
    self.service_account = service_account
    self.enable_main_authorized_networks = enable_main_authorized_networks
    self.main_authorized_networks = main_authorized_networks
    self.enable_legacy_authorization = enable_legacy_authorization
    self.enable_network_policy = enable_network_policy
    self.labels = labels
    self.disk_type = disk_type
    self.services_ipv4_cidr = services_ipv4_cidr
    self.enable_ip_alias = enable_ip_alias
    self.create_subnetwork = create_subnetwork
    self.cluster_secondary_range_name = cluster_secondary_range_name
    self.services_secondary_range_name = services_secondary_range_name
    self.enable_shared_network = enable_shared_network
    self.accelerators = accelerators
    self.enable_binauthz = enable_binauthz
    self.min_cpu_platform = min_cpu_platform
    self.workload_metadata_from_node = workload_metadata_from_node
    self.maintenance_window = maintenance_window
    self.enable_pod_security_policy = enable_pod_security_policy
    self.allow_route_overlap = allow_route_overlap
    self.private_cluster = private_cluster
    self.main_ipv4_cidr = main_ipv4_cidr
    self.tpu_ipv4_cidr = tpu_ipv4_cidr
    self.enable_tpu = enable_tpu


class UpdateClusterOptions(object):

  def __init__(self,
               version=None,
               update_main=None,
               update_nodes=None,
               node_pool=None,
               monitoring_service=None,
               disable_addons=None,
               enable_autoscaling=None,
               min_nodes=None,
               max_nodes=None,
               image_type=None,
               locations=None,
               enable_main_authorized_networks=None,
               main_authorized_networks=None,
               enable_autoprovisioning=None,
               enable_pod_security_policy=None,
               enable_binauthz=None):
    self.version = version
    self.update_main = bool(update_main)
    self.update_nodes = bool(update_nodes)
    self.node_pool = node_pool
    self.monitoring_service = monitoring_service
    self.disable_addons = disable_addons
    self.enable_autoscaling = enable_autoscaling
    self.min_nodes = min_nodes
    self.max_nodes = max_nodes
    self.image_type = image_type
    self.locations = locations
    self.enable_main_authorized_networks = enable_main_authorized_networks
    self.main_authorized_networks = main_authorized_networks
    self.enable_autoprovisioning = enable_autoprovisioning
    self.enable_pod_security_policy = enable_pod_security_policy
    self.enable_binauthz = enable_binauthz


class SetMainAuthOptions(object):
  """Options to pass to SetMainAuth."""

  SET_PASSWORD = 'SetPassword'
  GENERATE_PASSWORD = 'GeneratePassword'
  SET_USERNAME = 'SetUsername'

  def __init__(self, action=None, username=None, password=None):
    self.action = action
    self.username = username
    self.password = password


class SetNetworkPolicyOptions(object):

  def __init__(self, enabled):
    self.enabled = enabled


class CreateNodePoolOptions(object):

  def __init__(self,
               machine_type=None,
               disk_size_gb=None,
               scopes=None,
               node_version=None,
               enable_cloud_endpoints=None,
               new_scopes_behavior=None,
               num_nodes=None,
               local_ssd_count=None,
               local_ssd_volume_configs=None,
               tags=None,
               node_labels=None,
               node_taints=None,
               enable_autoscaling=None,
               max_nodes=None,
               min_nodes=None,
               enable_autoprovisioning=None,
               image_type=None,
               preemptible=None,
               enable_autorepair=None,
               enable_autoupgrade=None,
               service_account=None,
               disk_type=None,
               accelerators=None,
               min_cpu_platform=None,
               workload_metadata_from_node=None):
    self.machine_type = machine_type
    self.disk_size_gb = disk_size_gb
    self.scopes = scopes
    self.node_version = node_version
    self.enable_cloud_endpoints = enable_cloud_endpoints
    self.new_scopes_behavior = new_scopes_behavior
    self.num_nodes = num_nodes
    self.local_ssd_count = local_ssd_count
    self.local_ssd_volume_configs = local_ssd_volume_configs
    self.tags = tags
    self.node_labels = node_labels
    self.node_taints = node_taints
    self.enable_autoscaling = enable_autoscaling
    self.max_nodes = max_nodes
    self.min_nodes = min_nodes
    self.enable_autoprovisioning = enable_autoprovisioning
    self.image_type = image_type
    self.preemptible = preemptible
    self.enable_autorepair = enable_autorepair
    self.enable_autoupgrade = enable_autoupgrade
    self.service_account = service_account
    self.disk_type = disk_type
    self.accelerators = accelerators
    self.min_cpu_platform = min_cpu_platform
    self.workload_metadata_from_node = workload_metadata_from_node


class UpdateNodePoolOptions(object):
  """Options to pass to UpdateNodePool."""

  def __init__(self,
               enable_autorepair=None,
               enable_autoupgrade=None,
               enable_autoscaling=None,
               max_nodes=None,
               min_nodes=None,
               enable_autoprovisioning=None):
    self.enable_autorepair = enable_autorepair
    self.enable_autoupgrade = enable_autoupgrade
    self.enable_autoscaling = enable_autoscaling
    self.max_nodes = max_nodes
    self.min_nodes = min_nodes
    self.enable_autoprovisioning = enable_autoprovisioning

  def IsAutoscalingUpdate(self):
    return (self.enable_autoscaling is not None or
            self.max_nodes is not None or
            self.min_nodes is not None or
            self.enable_autoprovisioning is not None)


class APIAdapter(object):
  """Handles making api requests in a version-agnostic way."""

  def __init__(self, registry, client, messages):
    self.registry = registry
    self.client = client
    self.messages = messages

  def ParseCluster(self, name, location):
    # TODO(b/63383536): Migrate to container.projects.locations.clusters when
    # apiserver supports it.
    return self.registry.Parse(
        name,
        params={
            'projectId': properties.VALUES.core.project.GetOrFail,
            'zone': location,
        },
        collection='container.projects.zones.clusters')

  def ParseOperation(self, operation_id, location):
    # TODO(b/63383536): Migrate to container.projects.locations.operations when
    # apiserver supports it.
    return self.registry.Parse(
        operation_id,
        params={
            'projectId': properties.VALUES.core.project.GetOrFail,
            'zone': location,
        },
        collection='container.projects.zones.operations')

  def ParseNodePool(self, node_pool_id, location):
    # TODO(b/63383536): Migrate to container.projects.locations.nodePools when
    # apiserver supports it.
    return self.registry.Parse(
        node_pool_id,
        params={
            'projectId': properties.VALUES.core.project.GetOrFail,
            'clusterId': properties.VALUES.container.cluster.GetOrFail,
            'zone': location,
        },
        collection='container.projects.zones.clusters.nodePools')

  def GetCluster(self, cluster_ref):
    raise NotImplementedError('GetCluster is not overridden')

  def TryToGetCluster(self, cluster_ref, api_error):
    """Try to get cluster in all zones to see if there is a match."""
    try:
      clusters = self.ListClusters(cluster_ref.projectId).clusters
    except apitools_exceptions.HttpError as error:
      raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)
    for cluster in clusters:
      if cluster.name == cluster_ref.clusterId:
        # User likely got zone wrong.
        raise util.Error(WRONG_ZONE_ERROR_MSG.format(
            error=api_error,
            name=cluster_ref.clusterId,
            wrong_zone=self.Zone(cluster_ref),
            zone=cluster.zone))
    # Couldn't find a cluster with that name.
    raise util.Error(NO_SUCH_CLUSTER_ERROR_MSG.format(
        error=api_error,
        name=cluster_ref.clusterId,
        project=cluster_ref.projectId))

  def FindNodePool(self, cluster, pool_name=None):
    """Find the node pool with the given name in the cluster."""
    msg = ''
    if pool_name:
      for np in cluster.nodePools:
        if np.name == pool_name:
          return np
      msg = NO_SUCH_NODE_POOL_ERROR_MSG.format(cluster=cluster.name,
                                               name=pool_name) + linesep
    elif len(cluster.nodePools) == 1:
      return cluster.nodePools[0]
    # Couldn't find a node pool with that name or a node pool was not specified.
    msg += NO_NODE_POOL_SELECTED_ERROR_MSG + linesep.join(
        [np.name for np in cluster.nodePools])
    raise util.Error(msg)

  def GetOperation(self, operation_ref):
    raise NotImplementedError('GetOperation is not overridden')

  def WaitForOperation(self, operation_ref, message,
                       timeout_s=1200, poll_period_s=5):
    """Poll container Operation until its status is done or timeout reached.

    Args:
      operation_ref: operation resource.
      message: str, message to display to user while polling.
      timeout_s: number, seconds to poll with retries before timing out.
      poll_period_s: number, delay in seconds between requests.

    Returns:
      Operation: the return value of the last successful operations.get
      request.

    Raises:
      Error: if the operation times out or finishes with an error.
    """
    detail_message = None
    with progress_tracker.ProgressTracker(message, autotick=True,
                                          detail_message_callback=
                                          lambda: detail_message):
      start_time = time.clock()
      while timeout_s > (time.clock() - start_time):
        try:
          operation = self.GetOperation(operation_ref)
          if self.IsOperationFinished(operation):
            # Success!
            log.info('Operation %s succeeded after %.3f seconds',
                     operation, (time.clock() - start_time))
            break
          detail_message = operation.detail
        except apitools_exceptions.HttpError as error:
          log.debug('GetOperation failed: %s', error)
          if error.status_code == httplib.FORBIDDEN:
            raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)
          # Keep trying until we timeout in case error is transient.
          # TODO(b/36050880): add additional backoff if server is returning 500s
        time.sleep(poll_period_s)
    if not self.IsOperationFinished(operation):
      log.err.Print('Timed out waiting for operation {0}'.format(operation))
      raise util.Error(
          'Operation [{0}] is still running'.format(operation))
    if self.GetOperationError(operation):
      raise util.Error('Operation [{0}] finished with error: {1}'.format(
          operation, self.GetOperationError(operation)))

    return operation

  def Zone(self, cluster_ref):
    return cluster_ref.zone

  def CreateClusterCommon(self, cluster_ref, options):
    """Returns a CreateCluster operation."""
    node_config = self.messages.NodeConfig()
    if options.node_machine_type:
      node_config.machineType = options.node_machine_type
    if options.node_disk_size_gb:
      node_config.diskSizeGb = options.node_disk_size_gb
    if options.disk_type:
      node_config.diskType = options.disk_type
    if options.node_source_image:
      raise util.Error('cannot specify node source image in container v1 api')

    NodeIdentityOptionsToNodeConfig(options, node_config)

    if options.local_ssd_count:
      node_config.localSsdCount = options.local_ssd_count

    if options.tags:
      node_config.tags = options.tags
    else:
      node_config.tags = []

    if options.image_type:
      node_config.imageType = options.image_type

    _AddNodeLabelsToNodeConfig(node_config, options)
    self._AddNodeTaintsToNodeConfig(node_config, options)

    if options.preemptible:
      node_config.preemptible = options.preemptible

    if options.accelerators is not None:
      type_name = options.accelerators['type']
      # Accelerator count defaults to 1.
      count = int(options.accelerators.get('count', 1))
      node_config.accelerators = [
          self.messages.AcceleratorConfig(acceleratorType=type_name,
                                          acceleratorCount=count)
      ]

    if options.min_cpu_platform is not None:
      node_config.minCpuPlatform = options.min_cpu_platform

    _AddWorkloadMetadataToNodeConfig(node_config, options, self.messages)

    max_nodes_per_pool = options.max_nodes_per_pool or MAX_NODES_PER_POOL
    pools = (options.num_nodes + max_nodes_per_pool - 1) / max_nodes_per_pool
    if pools == 1:
      pool_names = ['default-pool']  # pool consistency with server default
    else:
      # default-pool-0, -1, ...
      pool_names = ['default-pool-{0}'.format(i) for i in range(0, pools)]

    pools = []
    per_pool = (options.num_nodes + len(pool_names) - 1) / len(pool_names)
    to_add = options.num_nodes
    for name in pool_names:
      nodes = per_pool if (to_add > per_pool) else to_add
      autoscaling = None
      if options.enable_autoscaling:
        autoscaling = self.messages.NodePoolAutoscaling(
            enabled=options.enable_autoscaling,
            minNodeCount=options.min_nodes,
            maxNodeCount=options.max_nodes)
      pools.append(
          self.messages.NodePool(
              name=name,
              initialNodeCount=nodes,
              config=node_config,
              autoscaling=autoscaling,
              version=options.node_version,
              management=self._GetNodeManagement(options)))
      to_add -= nodes

    cluster = self.messages.Cluster(
        name=cluster_ref.clusterId,
        nodePools=pools,
        mainAuth=self.messages.MainAuth(username=options.user,
                                            password=options.password))
    if options.additional_zones:
      cluster.locations = sorted([cluster_ref.zone] + options.additional_zones)
    if options.node_locations:
      cluster.locations = sorted(options.node_locations)
    if options.cluster_version:
      cluster.initialClusterVersion = options.cluster_version
    if options.network:
      cluster.network = options.network
    if options.cluster_ipv4_cidr:
      cluster.clusterIpv4Cidr = options.cluster_ipv4_cidr
    if not options.enable_cloud_logging:
      cluster.loggingService = 'none'
    if not options.enable_cloud_monitoring:
      cluster.monitoringService = 'none'
    if options.subnetwork:
      cluster.subnetwork = options.subnetwork
    if options.disable_addons:
      addons = self._AddonsConfig(
          disable_ingress=INGRESS in options.disable_addons or None,
          disable_hpa=HPA in options.disable_addons or None,
          disable_dashboard=DASHBOARD in options.disable_addons or None,
          disable_network_policy=(
              NETWORK_POLICY in options.disable_addons or None))
      cluster.addonsConfig = addons
    if options.addons:
      addons = self._AddonsConfig(
          disable_ingress=INGRESS not in options.addons,
          disable_hpa=HPA not in options.addons,
          disable_dashboard=DASHBOARD not in options.addons,
          disable_network_policy=(
              NETWORK_POLICY not in options.addons))
      cluster.addonsConfig = addons
    if options.enable_main_authorized_networks:
      authorized_networks = self.messages.MainAuthorizedNetworksConfig(
          enabled=options.enable_main_authorized_networks)
      if options.main_authorized_networks:
        for network in options.main_authorized_networks:
          authorized_networks.cidrBlocks.append(self.messages.CidrBlock(
              cidrBlock=network))
      cluster.mainAuthorizedNetworksConfig = authorized_networks
    elif options.main_authorized_networks:
      # Raise error if use --main-authorized-networks without
      # --enable-main-authorized-networks.
      raise util.Error(MISMATCH_AUTHORIZED_NETWORKS_ERROR_MSG)

    if options.enable_kubernetes_alpha:
      cluster.enableKubernetesAlpha = options.enable_kubernetes_alpha

    if options.enable_legacy_authorization is not None:
      cluster.legacyAbac = self.messages.LegacyAbac(
          enabled=bool(options.enable_legacy_authorization))

    # Only Calico is currently supported as a network policy provider.
    if options.enable_network_policy:
      cluster.networkPolicy = self.messages.NetworkPolicy(
          enabled=options.enable_network_policy,
          provider=self.messages.NetworkPolicy.ProviderValueValuesEnum.CALICO)

    if options.enable_binauthz is not None:
      cluster.binaryAuthorization = self.messages.BinaryAuthorization(
          enabled=options.enable_binauthz)

    if options.maintenance_window is not None:
      policy = self.messages.MaintenancePolicy(
          window=self.messages.MaintenanceWindow(
              dailyMaintenanceWindow=self.messages.DailyMaintenanceWindow(
                  startTime=options.maintenance_window)))
      cluster.maintenancePolicy = policy

    if options.labels is not None:
      labels = self.messages.Cluster.ResourceLabelsValue()
      props = []
      for k, v in sorted(options.labels.iteritems()):
        props.append(labels.AdditionalProperty(key=k, value=v))
      labels.additionalProperties = props
      cluster.resourceLabels = labels

    if options.enable_pod_security_policy is not None:
      cluster.podSecurityPolicyConfig = self.messages.PodSecurityPolicyConfig(
          enabled=options.enable_pod_security_policy)

    self.ParseNetworkConfigOptions(options, cluster)
    self.ParseIPAliasOptions(options, cluster)
    self.ParseAllowRouteOverlapOptions(options, cluster)
    self.ParsePrivateClusterOptions(options, cluster)
    self.ParseTpuOptions(options, cluster)
    return cluster

  def ParseNetworkConfigOptions(self, options, cluster):
    """Asserts the options for Shared VPC Networking."""
    if not options.enable_shared_network:
      return cluster

    req_related_options = [('enable-kubernetes-alpha',
                            options.enable_kubernetes_alpha),
                           ('enable-ip-alias',
                            options.enable_ip_alias), ('subnetwork',
                                                       options.subnetwork),
                           ('cluster-secondary-range-name',
                            options.cluster_secondary_range_name),
                           ('services-secondary-range-name',
                            options.services_secondary_range_name)]

    for name, opt in req_related_options:
      if not opt:
        raise util.MissingArgForSharedSubnetError(name)

    network_config = self.messages.NetworkConfig(
        enableSharedNetwork=options.enable_shared_network)
    cluster.networkConfig = network_config
    return cluster

  def ParseIPAliasOptions(self, options, cluster):
    """Parses the options for IP Alias."""
    ip_alias_only_options = [('services-ipv4-cidr', options.services_ipv4_cidr),
                             ('create-subnetwork', options.create_subnetwork),
                             ('cluster-secondary-range-name',
                              options.cluster_secondary_range_name),
                             ('services-secondary-range-name',
                              options.services_secondary_range_name)]
    if not options.enable_ip_alias:
      for name, opt in ip_alias_only_options:
        if opt:
          raise util.Error(PREREQUISITE_OPTION_ERROR_MSG.format(
              prerequisite='enable-ip-alias', opt=name))

    if options.subnetwork and options.create_subnetwork is not None:
      raise util.Error(CREATE_SUBNETWORK_WITH_SUBNETWORK_ERROR_MSG)
    if options.create_subnetwork is not None and (
        options.cluster_secondary_range_name or
        options.services_secondary_range_name):
      raise util.Error(
          CREATE_SUBNETWORK_WITH_EXPLICIT_SECONDARY_RANGES_ERROR_MSG)
    if ((options.cluster_secondary_range_name and
         not options.services_secondary_range_name) or
        (not options.cluster_secondary_range_name and
         options.services_secondary_range_name)):
      raise util.Error(MISSING_EXPLICIT_SECONDARY_RANGE_ERROR_MSG)

    if options.enable_ip_alias:
      subnetwork_name = None
      node_ipv4_cidr = None

      if options.create_subnetwork is not None:
        for key in options.create_subnetwork:
          if key not in ['name', 'range']:
            raise util.Error(
                CREATE_SUBNETWORK_INVALID_KEY_ERROR_MSG.format(key=key))
        subnetwork_name = options.create_subnetwork.get('name', None)
        node_ipv4_cidr = options.create_subnetwork.get('range', None)

      policy = self.messages.IPAllocationPolicy(
          useIpAliases=options.enable_ip_alias,
          createSubnetwork=options.create_subnetwork is not None,
          subnetworkName=subnetwork_name,
          clusterIpv4CidrBlock=options.cluster_ipv4_cidr,
          nodeIpv4CidrBlock=node_ipv4_cidr,
          servicesIpv4CidrBlock=options.services_ipv4_cidr,
          clusterSecondaryRangeName=options.cluster_secondary_range_name,
          servicesSecondaryRangeName=options.services_secondary_range_name)
      if options.tpu_ipv4_cidr:
        policy.tpuIpv4CidrBlock = options.tpu_ipv4_cidr
      cluster.clusterIpv4Cidr = None
      cluster.ipAllocationPolicy = policy
    return cluster

  def ParseAllowRouteOverlapOptions(self, options, cluster):
    """Parse the options for allow route overlap."""
    if not options.allow_route_overlap:
      return
    # Validate required flags are set.
    if options.cluster_ipv4_cidr is None:
      raise util.Error(ALLOW_ROUTE_OVERLAP_WITHOUT_CLUSTER_CIDR_ERROR_MSG)
    if options.enable_ip_alias and options.services_ipv4_cidr is None:
      raise util.Error(ALLOW_ROUTE_OVERLAP_WITHOUT_SERVICES_CIDR_ERROR_MSG)

    # Fill in corresponding field.
    if cluster.ipAllocationPolicy is None:
      policy = self.messages.IPAllocationPolicy(
          allowRouteOverlap=True)
      cluster.ipAllocationPolicy = policy
    else:
      cluster.ipAllocationPolicy.allowRouteOverlap = True

  def ParsePrivateClusterOptions(self, options, cluster):
    """Parses the options for Private Clusters."""
    if options.private_cluster:
      cluster.mainIpv4CidrBlock = options.main_ipv4_cidr
      cluster.privateCluster = options.private_cluster
    return cluster

  def ParseTpuOptions(self, options, cluster):
    """Parses the options for TPUs."""
    if options.enable_tpu and not options.enable_kubernetes_alpha:
      # Raises error if use --enable-tpu without --enable-kubernetes-alpha.
      raise util.Error(
          PREREQUISITE_OPTION_ERROR_MSG.format(
              prerequisite='enable-kubernetes-alpha', opt='enable-tpu'))

    if options.enable_tpu and not options.enable_ip_alias:
      # Raises error if use --enable-tpu without --enable-ip-alias.
      raise util.Error(
          PREREQUISITE_OPTION_ERROR_MSG.format(
              prerequisite='enable-ip-alias', opt='enable-tpu'))

    if not options.enable_tpu and options.tpu_ipv4_cidr:
      # Raises error if use --tpu-ipv4-cidr without --enable-tpu.
      raise util.Error(
          PREREQUISITE_OPTION_ERROR_MSG.format(
              prerequisite='enable-tpu', opt='tpu-ipv4-cidr'))

    if options.enable_tpu:
      cluster.enableTpu = options.enable_tpu

  def CreateCluster(self, cluster_ref, options):
    raise NotImplementedError('CreateCluster is not overridden')

  def CreateClusterAutoscalingCommon(self, _):
    raise util.Error(NO_AUTOPROVISIONING_MSG)

  def UpdateClusterCommon(self, options):
    """Returns an UpdateCluster operation."""
    if not options.version:
      options.version = '-'
    if options.update_nodes:
      update = self.messages.ClusterUpdate(
          desiredNodeVersion=options.version,
          desiredNodePoolId=options.node_pool,
          desiredImageType=options.image_type)
    elif options.update_main:
      update = self.messages.ClusterUpdate(
          desiredMainVersion=options.version)
    elif options.monitoring_service:
      update = self.messages.ClusterUpdate(
          desiredMonitoringService=options.monitoring_service)
    elif options.disable_addons:
      addons = self._AddonsConfig(
          disable_ingress=options.disable_addons.get(INGRESS),
          disable_hpa=options.disable_addons.get(HPA),
          disable_dashboard=options.disable_addons.get(DASHBOARD),
          disable_network_policy=options.disable_addons.get(NETWORK_POLICY))
      update = self.messages.ClusterUpdate(desiredAddonsConfig=addons)
    elif options.enable_autoscaling is not None:
      # For update, we can either enable or disable.
      autoscaling = self.messages.NodePoolAutoscaling(
          enabled=options.enable_autoscaling)
      if options.enable_autoscaling:
        autoscaling.minNodeCount = options.min_nodes
        autoscaling.maxNodeCount = options.max_nodes
      update = self.messages.ClusterUpdate(
          desiredNodePoolId=options.node_pool,
          desiredNodePoolAutoscaling=autoscaling)
    elif options.locations:
      update = self.messages.ClusterUpdate(desiredLocations=options.locations)
    elif options.enable_main_authorized_networks is not None:
      # For update, we can either enable or disable.
      authorized_networks = self.messages.MainAuthorizedNetworksConfig(
          enabled=options.enable_main_authorized_networks)
      if options.main_authorized_networks:
        for network in options.main_authorized_networks:
          authorized_networks.cidrBlocks.append(self.messages.CidrBlock(
              cidrBlock=network))
      update = self.messages.ClusterUpdate(
          desiredMainAuthorizedNetworksConfig=authorized_networks)
    elif options.enable_autoprovisioning is not None:
      autoscaling = self.CreateClusterAutoscalingCommon(options)
      update = self.messages.ClusterUpdate(
          desiredClusterAutoscaling=autoscaling)
    elif options.enable_pod_security_policy is not None:
      config = self.messages.PodSecurityPolicyConfig(
          enabled=options.enable_pod_security_policy)
      update = self.messages.ClusterUpdate(
          desiredPodSecurityPolicyConfig=config)
    elif options.enable_binauthz is not None:
      binary_authorization = self.messages.BinaryAuthorization(
          enabled=options.enable_binauthz)
      update = self.messages.ClusterUpdate(
          desiredBinaryAuthorization=binary_authorization)

    if (options.main_authorized_networks
        and not options.enable_main_authorized_networks):
      # Raise error if use --main-authorized-networks without
      # --enable-main-authorized-networks.
      raise util.Error(MISMATCH_AUTHORIZED_NETWORKS_ERROR_MSG)
    return update

  def UpdateCluster(self, cluster_ref, options):
    raise NotImplementedError('UpdateCluster is not overridden')

  def SetLoggingService(self, cluster_ref, logging_service):
    raise NotImplementedError('SetLoggingService is not overridden')

  def SetLegacyAuthorization(self, cluster_ref, enable_legacy_authorization):
    raise NotImplementedError('SetLegacyAuthorization is not overridden')

  def _AddonsConfig(self,
                    disable_ingress=None,
                    disable_hpa=None,
                    disable_dashboard=None,
                    disable_network_policy=None):
    """Generates an AddonsConfig object given specific parameters.

    Args:
      disable_ingress: whether to disable the GCLB ingress controller.
      disable_hpa: whether to disable the horizontal pod autoscaling controller.
      disable_dashboard: whether to disable the Kuberntes Dashboard.
      disable_network_policy: whether to disable NetworkPolicy enforcement.

    Returns:
      An AddonsConfig object that contains the options defining what addons to
      run in the cluster.
    """
    addons = self.messages.AddonsConfig()
    if disable_ingress is not None:
      addons.httpLoadBalancing = self.messages.HttpLoadBalancing(
          disabled=disable_ingress)
    if disable_hpa is not None:
      addons.horizontalPodAutoscaling = self.messages.HorizontalPodAutoscaling(
          disabled=disable_hpa)
    if disable_dashboard is not None:
      addons.kubernetesDashboard = self.messages.KubernetesDashboard(
          disabled=disable_dashboard)
    # Network policy is disabled by default.
    if disable_network_policy is not None:
      addons.networkPolicyConfig = self.messages.NetworkPolicyConfig(
          disabled=disable_network_policy)
    return addons

  def _AddLocalSSDVolumeConfigsToNodeConfig(self, node_config, options):
    """Add LocalSSDVolumeConfigs to nodeConfig."""
    if options.local_ssd_volume_configs is None:
      return
    format_enum = self.messages.LocalSsdVolumeConfig.FormatValueValuesEnum
    local_ssd_volume_configs_list = []
    for config in options.local_ssd_volume_configs:
      count = int(config['count'])
      ssd_type = config['type'].lower()
      if config['format'].lower() == 'fs':
        ssd_format = format_enum.FS
      elif config['format'].lower() == 'block':
        ssd_format = format_enum.BLOCK
      else:
        raise util.Error(
            LOCAL_SSD_INCORRECT_FORMAT_ERROR_MSG.format(
                err_format=config['format']))
      local_ssd_volume_configs_list.append(
          self.messages.LocalSsdVolumeConfig(
              count=count, type=ssd_type, format=ssd_format))
    node_config.localSsdVolumeConfigs = local_ssd_volume_configs_list

  def _AddNodeTaintsToNodeConfig(self, node_config, options):
    """Add nodeTaints to nodeConfig."""
    if options.node_taints is None:
      return
    taints = []
    effect_enum = self.messages.NodeTaint.EffectValueValuesEnum
    for key, value in sorted(options.node_taints.iteritems()):
      strs = value.split(':')
      if len(strs) != 2:
        raise util.Error(
            NODE_TAINT_INCORRECT_FORMAT_ERROR_MSG.format(key=key, value=value))
      value = strs[0]
      taint_effect = strs[1]
      if taint_effect == 'NoSchedule':
        effect = effect_enum.NO_SCHEDULE
      elif taint_effect == 'PreferNoSchedule':
        effect = effect_enum.PREFER_NO_SCHEDULE
      elif taint_effect == 'NoExecute':
        effect = effect_enum.NO_EXECUTE
      else:
        raise util.Error(
            NODE_TAINT_INCORRECT_EFFECT_ERROR_MSG.format(effect=strs[1]))
      taints.append(self.messages.NodeTaint(
          key=key, value=value, effect=effect))

    node_config.taints = taints

  def SetNetworkPolicyCommon(self, options):
    """Returns a SetNetworkPolicy operation."""
    return self.messages.NetworkPolicy(
        enabled=options.enabled,
        # Only Calico is currently supported as a network policy provider.
        provider=self.messages.NetworkPolicy.ProviderValueValuesEnum.CALICO)

  def SetNetworkPolicy(self, cluster_ref, options):
    raise NotImplementedError('SetNetworkPolicy is not overridden')

  def SetMainAuthCommon(self, options):
    """Returns a SetMainAuth action."""
    update = self.messages.MainAuth(
        username=options.username, password=options.password)
    if options.action == SetMainAuthOptions.SET_PASSWORD:
      action = (self.messages.SetMainAuthRequest.
                ActionValueValuesEnum.SET_PASSWORD)
    elif options.action == SetMainAuthOptions.GENERATE_PASSWORD:
      action = (self.messages.SetMainAuthRequest.
                ActionValueValuesEnum.GENERATE_PASSWORD)
    else:  # options.action == SetMainAuthOptions.SET_USERNAME
      action = (
          self.messages.SetMainAuthRequest.ActionValueValuesEnum.SET_USERNAME)
    return update, action

  def SetMainAuth(self, cluster_ref, options):
    raise NotImplementedError('SetMainAuth is not overridden')

  def StartIpRotation(self, cluster_ref):
    raise NotImplementedError('StartIpRotation is not overridden')

  def CompleteIpRotation(self, cluster_ref):
    raise NotImplementedError('CompleteIpRotation is not overridden')

  def SetMaintenanceWindow(self, cluster_ref, maintenance_window):
    raise NotImplementedError('SetMaintenanceWindow is not overridden')

  def DeleteCluster(self, cluster_ref):
    raise NotImplementedError('DeleteCluster is not overridden')

  def ListClusters(self, project, zone=None):
    raise NotImplementedError('ListClusters is not overridden')

  def CreateNodePoolCommon(self, node_pool_ref, options):
    """Returns a CreateNodePool operation."""
    node_config = self.messages.NodeConfig()
    if options.machine_type:
      node_config.machineType = options.machine_type
    if options.disk_size_gb:
      node_config.diskSizeGb = options.disk_size_gb
    if options.disk_type:
      node_config.diskType = options.disk_type
    if options.image_type:
      node_config.imageType = options.image_type

    NodeIdentityOptionsToNodeConfig(options, node_config)

    if options.local_ssd_count:
      node_config.localSsdCount = options.local_ssd_count
    if options.local_ssd_volume_configs:
      self._AddLocalSSDVolumeConfigsToNodeConfig(node_config, options)
    if options.tags:
      node_config.tags = options.tags
    else:
      node_config.tags = []

    if options.accelerators is not None:
      type_name = options.accelerators['type']
      # Accelerator count defaults to 1.
      count = int(options.accelerators.get('count', 1))
      node_config.accelerators = [
          self.messages.AcceleratorConfig(acceleratorType=type_name,
                                          acceleratorCount=count)
      ]

    _AddNodeLabelsToNodeConfig(node_config, options)
    self._AddNodeTaintsToNodeConfig(node_config, options)

    if options.preemptible:
      node_config.preemptible = options.preemptible

    if options.min_cpu_platform is not None:
      node_config.minCpuPlatform = options.min_cpu_platform

    if options.workload_metadata_from_node:
      node_config.workloadMetadataConfig = self.messages.WorkloadMetadataConfig(
          nodeMetadata=self.messages.WorkloadMetadataConfig.
          NodeMetadataValueValuesEnum.SECURE)

    pool = self.messages.NodePool(
        name=node_pool_ref.nodePoolId,
        initialNodeCount=options.num_nodes,
        config=node_config,
        version=options.node_version,
        management=self._GetNodeManagement(options))

    if options.enable_autoscaling:
      pool.autoscaling = self.messages.NodePoolAutoscaling(
          enabled=options.enable_autoscaling,
          minNodeCount=options.min_nodes,
          maxNodeCount=options.max_nodes)
    return pool

  def CreateNodePool(self, node_pool_ref, options):
    raise NotImplementedError('CreateNodePool is not overridden')

  def ListNodePools(self, cluster_ref):
    raise NotImplementedError('ListNodePools is not overridden')

  def GetNodePool(self, node_pool_ref):
    raise NotImplementedError('GetNodePool is not overridden')

  def UpdateNodePoolNodeManagement(self, node_pool_ref, options):
    """Update node pool's node management configuration.

    Args:
      node_pool_ref: node pool Resource to update.
      options: node pool update options
    Returns:
      Updated node management configuration.
    """
    pool = self.GetNodePool(node_pool_ref)
    node_management = pool.management
    if node_management is None:
      node_management = self.messages.NodeManagement()
    if options.enable_autorepair is not None:
      node_management.autoRepair = options.enable_autorepair
    if options.enable_autoupgrade is not None:
      node_management.autoUpgrade = options.enable_autoupgrade
    return node_management

  def UpdateNodePoolAutoscaling(self, node_pool_ref, options):
    """Update node pool's autoscaling configuration.

    Args:
      node_pool_ref: node pool Resource to update.
      options: node pool update options
    Returns:
      Updated autoscaling configuration for the node pool.
    """
    pool = self.GetNodePool(node_pool_ref)
    autoscaling = pool.autoscaling
    if autoscaling is None:
      autoscaling = self.messages.NodePoolAutoscaling()
    if options.enable_autoscaling is not None:
      autoscaling.enabled = options.enable_autoscaling
    if options.max_nodes is not None:
      autoscaling.maxNodeCount = options.max_nodes
    if options.min_nodes is not None:
      autoscaling.minNodeCount = options.min_nodes
    elif options.enable_autoprovisioning is not None:
      # clear min nodes limit when enabling autoprovisioning
      autoscaling.minNodeCount = 0
    if options.enable_autoprovisioning is not None:
      autoscaling.autoprovisioned = options.enable_autoprovisioning
    elif not autoscaling.enabled:
      # turn off autoprovisioning when disabling autoscaling
      autoscaling.autoprovisioned = False
    return autoscaling

  def UpdateNodePool(self, node_pool_ref, options):
    raise NotImplementedError('UpdateNodePool is not overridden')

  def DeleteNodePool(self, node_pool_ref):
    raise NotImplementedError('DeleteNodePool is not overridden')

  def RollbackUpgrade(self, node_pool_ref):
    raise NotImplementedError('RollbackUpgrade is not overridden')

  def CancelOperation(self, op_ref):
    raise NotImplementedError('CancelOperation is not overridden')

  def IsRunning(self, cluster):
    return (cluster.status ==
            self.messages.Cluster.StatusValueValuesEnum.RUNNING)

  def GetOperationError(self, operation):
    return operation.statusMessage

  def ListOperations(self, project, zone=None):
    raise NotImplementedError('ListOperations is not overridden')

  def IsOperationFinished(self, operation):
    return (operation.status ==
            self.messages.Operation.StatusValueValuesEnum.DONE)

  def GetServerConfig(self, project, zone):
    raise NotImplementedError('GetServerConfig is not overridden')

  def ResizeNodePool(self, cluster_ref, pool_name, size):
    raise NotImplementedError('ResizeNodePool is not overridden')

  def _GetNodeManagement(self, options):
    """Gets a wrapper containing the options for how nodes are managed.

    Args:
      options: node management options

    Returns:
      A NodeManagement object that contains the options indicating how nodes
      are managed. This is currently quite simple, containing only two options.
      However, there are more options planned for node management.
    """
    if options.enable_autorepair is None and options.enable_autoupgrade is None:
      return None

    node_management = self.messages.NodeManagement()
    node_management.autoRepair = options.enable_autorepair
    node_management.autoUpgrade = options.enable_autoupgrade
    return node_management

  def UpdateLabelsCommon(self, cluster_ref, update_labels):
    """Update labels on a cluster.

    Args:
      cluster_ref: cluster to update.
      update_labels: labels to set.
    Returns:
      Operation ref for label set operation.
    """
    clus = None
    try:
      clus = self.GetCluster(cluster_ref)
    except apitools_exceptions.HttpNotFoundError:
      pass
    except apitools_exceptions.HttpError as error:
      raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)

    labels = self.messages.SetLabelsRequest.ResourceLabelsValue()
    props = []
    for k, v in sorted(update_labels.iteritems()):
      props.append(labels.AdditionalProperty(key=k, value=v))
    labels.additionalProperties = props
    return labels, clus.labelFingerprint

  def UpdateLabels(self, cluster_ref, update_labels):
    raise NotImplementedError('UpdateLabels is not overridden')

  def RemoveLabelsCommon(self, cluster_ref, remove_labels):
    """Removes labels from a cluster.

    Args:
      cluster_ref: cluster to update.
      remove_labels: labels to remove.
    Returns:
      Operation ref for label set operation.
    """
    clus = None
    try:
      clus = self.GetCluster(cluster_ref)
    except apitools_exceptions.HttpNotFoundError:
      pass
    except apitools_exceptions.HttpError as error:
      raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)

    clus_labels = {}
    if clus.resourceLabels:
      for item in clus.resourceLabels.additionalProperties:
        clus_labels[item.key] = str(item.value)

    # if clusLabels empty, nothing to do
    if not clus_labels:
      raise util.Error(NO_LABELS_ON_CLUSTER_ERROR_MSG.format(cluster=clus.name))

    for k in remove_labels:
      try:
        clus_labels.pop(k)
      except KeyError as error:
        # if at least one label not found on cluster, raise error
        raise util.Error(
            NO_SUCH_LABEL_ERROR_MSG.format(cluster=clus.name, name=k))

    labels = self.messages.SetLabelsRequest.ResourceLabelsValue()
    for k, v in sorted(clus_labels.iteritems()):
      labels.additionalProperties.append(
          labels.AdditionalProperty(key=k, value=v))
    return labels, clus.labelFingerprint

  def RemoveLabels(self, cluster_ref, remove_labels):
    raise NotImplementedError('RemoveLabels is not overridden')


class V1Adapter(APIAdapter):
  """APIAdapter for v1."""

  def CreateCluster(self, cluster_ref, options):
    cluster = self.CreateClusterCommon(cluster_ref, options)
    req = self.messages.CreateClusterRequest(
        cluster=cluster, projectId=cluster_ref.projectId, zone=cluster_ref.zone)
    operation = self.client.projects_zones_clusters.Create(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def UpdateCluster(self, cluster_ref, options):
    update = self.UpdateClusterCommon(options)
    op = self.client.projects_zones_clusters.Update(
        self.messages.UpdateClusterRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId,
            update=update))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetLoggingService(self, cluster_ref, logging_service):
    op = self.client.projects_zones_clusters.Logging(
        self.messages.SetLoggingServiceRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId,
            loggingService=logging_service))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetNetworkPolicy(self, cluster_ref, options):
    netpol = self.SetNetworkPolicyCommon(options)
    req = self.messages.SetNetworkPolicyRequest(
        clusterId=cluster_ref.clusterId,
        zone=cluster_ref.zone,
        projectId=cluster_ref.projectId,
        networkPolicy=netpol)
    return self.ParseOperation(
        self.client.projects_zones_clusters.SetNetworkPolicy(req).name,
        cluster_ref.zone)

  def SetLegacyAuthorization(self, cluster_ref, enable_legacy_authorization):
    op = self.client.projects_zones_clusters.LegacyAbac(
        self.messages.SetLegacyAbacRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId,
            enabled=bool(enable_legacy_authorization)))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetMainAuth(self, cluster_ref, options):
    update, action = self.SetMainAuthCommon(options)
    req = self.messages.SetMainAuthRequest(
        clusterId=cluster_ref.clusterId,
        zone=cluster_ref.zone,
        projectId=cluster_ref.projectId,
        action=action,
        update=update)
    op = self.client.projects_zones_clusters.SetMainAuth(req)
    return self.ParseOperation(op.name, cluster_ref.zone)

  def StartIpRotation(self, cluster_ref):
    operation = self.client.projects_zones_clusters.StartIpRotation(
        self.messages.StartIPRotationRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def CompleteIpRotation(self, cluster_ref):
    operation = self.client.projects_zones_clusters.CompleteIpRotation(
        self.messages.CompleteIPRotationRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def GetCluster(self, cluster_ref):
    """Get a running cluster.

    Args:
      cluster_ref: cluster Resource to describe.
    Returns:
      Cluster message.
    Raises:
      Error: if cluster cannot be found.
    """
    try:
      return self.client.projects_zones_clusters.Get(
          self.messages.ContainerProjectsZonesClustersGetRequest(
              projectId=cluster_ref.projectId,
              zone=cluster_ref.zone,
              clusterId=cluster_ref.clusterId))
    except apitools_exceptions.HttpNotFoundError as error:
      api_error = exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)
      # Cluster couldn't be found, maybe user got zone wrong?
      self.TryToGetCluster(cluster_ref, api_error)
    except apitools_exceptions.HttpError as error:
      raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)

  def DeleteCluster(self, cluster_ref):
    operation = self.client.projects_zones_clusters.Delete(
        self.messages.ContainerProjectsZonesClustersDeleteRequest(
            clusterId=cluster_ref.clusterId,
            zone=cluster_ref.zone,
            projectId=cluster_ref.projectId))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def ListClusters(self, project, zone=None):
    if not zone:
      zone = '-'
    req = self.messages.ContainerProjectsZonesClustersListRequest(
        projectId=project, zone=zone)
    return self.client.projects_zones_clusters.List(req)

  def SetMaintenanceWindow(self, cluster_ref, maintenance_window):
    policy = self.messages.MaintenancePolicy(
        window=self.messages.MaintenanceWindow(
            dailyMaintenanceWindow=self.messages.DailyMaintenanceWindow(
                startTime=maintenance_window)))
    req = self.messages.SetMaintenancePolicyRequest(
        projectId=cluster_ref.projectId,
        zone=cluster_ref.zone,
        clusterId=cluster_ref.clusterId,
        maintenancePolicy=policy)
    if maintenance_window == 'None':
      req.maintenancePolicy = None

    operation = self.client.projects_zones_clusters.SetMaintenancePolicy(req)

    return self.ParseOperation(operation.name, cluster_ref.zone)

  def CreateNodePool(self, node_pool_ref, options):
    pool = self.CreateNodePoolCommon(node_pool_ref, options)
    req = self.messages.CreateNodePoolRequest(
        projectId=node_pool_ref.projectId,
        zone=node_pool_ref.zone,
        clusterId=node_pool_ref.clusterId,
        nodePool=pool)
    operation = self.client.projects_zones_clusters_nodePools.Create(req)
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def ListNodePools(self, cluster_ref):
    req = self.messages.ContainerProjectsZonesClustersNodePoolsListRequest(
        projectId=cluster_ref.projectId,
        zone=cluster_ref.zone,
        clusterId=cluster_ref.clusterId)
    return self.client.projects_zones_clusters_nodePools.List(req)

  def GetNodePool(self, node_pool_ref):
    req = self.messages.ContainerProjectsZonesClustersNodePoolsGetRequest(
        projectId=node_pool_ref.projectId,
        zone=node_pool_ref.zone,
        clusterId=node_pool_ref.clusterId,
        nodePoolId=node_pool_ref.nodePoolId)
    return self.client.projects_zones_clusters_nodePools.Get(req)

  def DeleteNodePool(self, node_pool_ref):
    operation = self.client.projects_zones_clusters_nodePools.Delete(
        self.messages.ContainerProjectsZonesClustersNodePoolsDeleteRequest(
            clusterId=node_pool_ref.clusterId,
            zone=node_pool_ref.zone,
            projectId=node_pool_ref.projectId,
            nodePoolId=node_pool_ref.nodePoolId))
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def UpdateNodePool(self, node_pool_ref, options):
    node_management = self.UpdateNodePoolNodeManagement(node_pool_ref, options)
    req = self.messages.SetNodePoolManagementRequest(
        projectId=node_pool_ref.projectId,
        zone=node_pool_ref.zone,
        clusterId=node_pool_ref.clusterId,
        nodePoolId=node_pool_ref.nodePoolId,
        management=node_management)
    operation = self.client.projects_zones_clusters_nodePools.SetManagement(req)
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def ResizeNodePool(self, cluster_ref, pool_name, size):
    """Sets the size of the node pool.

    Args:
      cluster_ref: cluster to update.
      pool_name: name of the node pool.
      size: size to set.
    Returns:
      Operation ref for resize operation.
    """
    req = self.messages.SetNodePoolSizeRequest(
        clusterId=cluster_ref.clusterId,
        nodePoolId=pool_name,
        projectId=cluster_ref.projectId,
        nodeCount=size,
        zone=cluster_ref.zone)
    operation = self.client.projects_zones_clusters_nodePools.SetSize(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def RollbackUpgrade(self, node_pool_ref):
    operation = self.client.projects_zones_clusters_nodePools.Rollback(
        self.messages.RollbackNodePoolUpgradeRequest(
            clusterId=node_pool_ref.clusterId,
            zone=node_pool_ref.zone,
            projectId=node_pool_ref.projectId,
            nodePoolId=node_pool_ref.nodePoolId))
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def CancelOperation(self, op_ref):
    req = self.messages.CancelOperationRequest(
        zone=op_ref.zone,
        projectId=op_ref.projectId,
        operationId=op_ref.operationId)
    return self.client.projects_zones_operations.Cancel(req)

  def ListOperations(self, project, zone=None):
    if not zone:
      zone = '-'
    req = self.messages.ContainerProjectsZonesOperationsListRequest(
        projectId=project, zone=zone)
    return self.client.projects_zones_operations.List(req)

  def GetOperation(self, operation_ref):
    return self.client.projects_zones_operations.Get(
        self.messages.ContainerProjectsZonesOperationsGetRequest(
            projectId=operation_ref.projectId,
            zone=operation_ref.zone,
            operationId=operation_ref.operationId))

  def GetServerConfig(self, project, zone):
    req = self.messages.ContainerProjectsZonesGetServerconfigRequest(
        projectId=project, zone=zone)
    return self.client.projects_zones.GetServerconfig(req)

  def UpdateLabels(self, cluster_ref, update_labels):
    labels, fingerprint = self.UpdateLabelsCommon(
        cluster_ref, update_labels)
    req = self.messages.SetLabelsRequest(
        clusterId=cluster_ref.clusterId,
        zone=cluster_ref.zone,
        projectId=cluster_ref.projectId,
        resourceLabels=labels,
        labelFingerprint=fingerprint)
    operation = self.client.projects_zones_clusters.ResourceLabels(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def RemoveLabels(self, cluster_ref, remove_labels):
    labels, fingerprint = self.RemoveLabelsCommon(
        cluster_ref, remove_labels)
    req = self.messages.SetLabelsRequest(
        clusterId=cluster_ref.clusterId,
        zone=cluster_ref.zone,
        projectId=cluster_ref.projectId,
        resourceLabels=labels,
        labelFingerprint=fingerprint)
    operation = self.client.projects_zones_clusters.ResourceLabels(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)


class V1Beta1Adapter(APIAdapter):
  """APIAdapter for v1beta1."""

  def CreateCluster(self, cluster_ref, options):
    cluster = self.CreateClusterCommon(cluster_ref, options)
    req = self.messages.CreateClusterRequest(
        parent=ProjectLocation(cluster_ref.projectId, cluster_ref.zone),
        cluster=cluster)
    operation = self.client.projects_locations_clusters.Create(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def UpdateCluster(self, cluster_ref, options):
    update = self.UpdateClusterCommon(options)
    op = self.client.projects_locations_clusters.Update(
        self.messages.UpdateClusterRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId),
            update=update))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetLoggingService(self, cluster_ref, logging_service):
    op = self.client.projects_locations_clusters.SetLogging(
        self.messages.SetLoggingServiceRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId),
            loggingService=logging_service))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetNetworkPolicy(self, cluster_ref, options):
    netpol = self.SetNetworkPolicyCommon(options)
    req = self.messages.SetNetworkPolicyRequest(
        name=ProjectLocationCluster(cluster_ref.projectId,
                                    cluster_ref.zone,
                                    cluster_ref.clusterId),
        networkPolicy=netpol)
    return self.ParseOperation(
        self.client.projects_locations_clusters.SetNetworkPolicy(req).name,
        cluster_ref.zone)

  def SetLegacyAuthorization(self, cluster_ref, enable_legacy_authorization):
    op = self.client.projects_locations_clusters.SetLegacyAbac(
        self.messages.SetLegacyAbacRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId),
            enabled=bool(enable_legacy_authorization)))
    return self.ParseOperation(op.name, cluster_ref.zone)

  def SetMainAuth(self, cluster_ref, options):
    update, action = self.SetMainAuthCommon(options)
    req = self.messages.SetMainAuthRequest(
        name=ProjectLocationCluster(cluster_ref.projectId,
                                    cluster_ref.zone,
                                    cluster_ref.clusterId),
        action=action,
        update=update)
    op = self.client.projects_locations_clusters.SetMainAuth(req)
    return self.ParseOperation(op.name, cluster_ref.zone)

  def StartIpRotation(self, cluster_ref):
    operation = self.client.projects_locations_clusters.StartIpRotation(
        self.messages.StartIPRotationRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId)))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def CompleteIpRotation(self, cluster_ref):
    operation = self.client.projects_locations_clusters.CompleteIpRotation(
        self.messages.CompleteIPRotationRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId)))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def GetCluster(self, cluster_ref):
    """Get a running cluster.

    Args:
      cluster_ref: cluster Resource to describe.
    Returns:
      Cluster message.
    Raises:
      Error: if cluster cannot be found.
    """
    try:
      return self.client.projects_locations_clusters.Get(
          self.messages.ContainerProjectsLocationsClustersGetRequest(
              name=ProjectLocationCluster(cluster_ref.projectId,
                                          cluster_ref.zone,
                                          cluster_ref.clusterId)))
    except apitools_exceptions.HttpNotFoundError as error:
      api_error = exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)
      # Cluster couldn't be found, maybe user got zone wrong?
      self.TryToGetCluster(cluster_ref, api_error)
    except apitools_exceptions.HttpError as error:
      raise exceptions.HttpException(error, util.HTTP_ERROR_FORMAT)

  def DeleteCluster(self, cluster_ref):
    operation = self.client.projects_locations_clusters.Delete(
        self.messages.ContainerProjectsLocationsClustersDeleteRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId)))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def ListClusters(self, project, zone=None):
    if not zone:
      zone = '-'
    req = self.messages.ContainerProjectsLocationsClustersListRequest(
        parent=ProjectLocation(project, zone))
    return self.client.projects_locations_clusters.List(req)

  def SetMaintenanceWindow(self, cluster_ref, maintenance_window):
    policy = self.messages.MaintenancePolicy(
        window=self.messages.MaintenanceWindow(
            dailyMaintenanceWindow=self.messages.DailyMaintenanceWindow(
                startTime=maintenance_window)))
    req = self.messages.SetMaintenancePolicyRequest(
        name=ProjectLocationCluster(cluster_ref.projectId,
                                    cluster_ref.zone,
                                    cluster_ref.clusterId),
        maintenancePolicy=policy)
    if maintenance_window == 'None':
      req.maintenancePolicy = None

    operation = self.client.projects_locations_clusters.SetMaintenancePolicy(
        req)

    return self.ParseOperation(operation.name, cluster_ref.zone)

  def CreateNodePool(self, node_pool_ref, options):
    pool = self.CreateNodePoolCommon(node_pool_ref, options)
    req = self.messages.CreateNodePoolRequest(
        nodePool=pool,
        parent=ProjectLocationCluster(node_pool_ref.projectId,
                                      node_pool_ref.zone,
                                      node_pool_ref.clusterId))
    operation = self.client.projects_locations_clusters_nodePools.Create(req)
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def ListNodePools(self, cluster_ref):
    req = self.messages.ContainerProjectsLocationsClustersNodePoolsListRequest(
        parent=ProjectLocationCluster(cluster_ref.projectId,
                                      cluster_ref.zone,
                                      cluster_ref.clusterId))
    return self.client.projects_locations_clusters_nodePools.List(req)

  def GetNodePool(self, node_pool_ref):
    req = self.messages.ContainerProjectsLocationsClustersNodePoolsGetRequest(
        name=ProjectLocationClusterNodePool(node_pool_ref.projectId,
                                            node_pool_ref.zone,
                                            node_pool_ref.clusterId,
                                            node_pool_ref.nodePoolId))
    return self.client.projects_locations_clusters_nodePools.Get(req)

  def DeleteNodePool(self, node_pool_ref):
    operation = self.client.projects_locations_clusters_nodePools.Delete(
        self.messages.ContainerProjectsLocationsClustersNodePoolsDeleteRequest(
            name=ProjectLocationClusterNodePool(
                node_pool_ref.projectId,
                node_pool_ref.zone,
                node_pool_ref.clusterId,
                node_pool_ref.nodePoolId)))
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def UpdateNodePool(self, node_pool_ref, options):
    node_management = self.UpdateNodePoolNodeManagement(node_pool_ref, options)
    req = (self.messages.SetNodePoolManagementRequest(
        name=ProjectLocationClusterNodePool(
            node_pool_ref.projectId,
            node_pool_ref.zone,
            node_pool_ref.clusterId,
            node_pool_ref.nodePoolId),
        management=node_management))
    operation = (
        self.client.projects_locations_clusters_nodePools.SetManagement(req))
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def ResizeNodePool(self, cluster_ref, pool_name, size):
    req = self.messages.SetNodePoolSizeRequest(
        name=ProjectLocationClusterNodePool(
            cluster_ref.projectId,
            cluster_ref.zone,
            cluster_ref.clusterId,
            pool_name),
        nodeCount=size
    )
    operation = self.client.projects_locations_clusters_nodePools.SetSize(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def RollbackUpgrade(self, node_pool_ref):
    operation = self.client.projects_locations_clusters_nodePools.Rollback(
        self.messages.RollbackNodePoolUpgradeRequest(
            name=ProjectLocationClusterNodePool(
                node_pool_ref.projectId,
                node_pool_ref.zone,
                node_pool_ref.clusterId,
                node_pool_ref.nodePoolId)))
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def CancelOperation(self, op_ref):
    req = self.messages.CancelOperationRequest(
        name=ProjectLocationOperation(op_ref.projectId,
                                      op_ref.zone,
                                      op_ref.operationId))
    return self.client.projects_locations_operations.Cancel(req)

  def ListOperations(self, project, zone=None):
    if not zone:
      zone = '-'
    req = self.messages.ContainerProjectsLocationsOperationsListRequest(
        parent=ProjectLocation(project, zone))
    return self.client.projects_locations_operations.List(req)

  def GetOperation(self, operation_ref):
    return self.client.projects_locations_operations.Get(
        self.messages.ContainerProjectsLocationsOperationsGetRequest(
            name=ProjectLocationOperation(operation_ref.projectId,
                                          operation_ref.zone,
                                          operation_ref.operationId)))

  def GetServerConfig(self, project, zone):
    req = self.messages.ContainerProjectsLocationsGetServerConfigRequest(
        name=ProjectLocation(project, zone))
    return self.client.projects_locations.GetServerConfig(req)

  def UpdateLabels(self, cluster_ref, update_labels):
    labels, fingerprint = self.UpdateLabelsCommon(
        cluster_ref, update_labels)
    operation = self.client.projects_locations_clusters.SetResourceLabels(
        self.messages.SetLabelsRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId),
            resourceLabels=labels,
            labelFingerprint=fingerprint))
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def RemoveLabels(self, cluster_ref, remove_labels):
    labels, fingerprint = self.RemoveLabelsCommon(
        cluster_ref, remove_labels)
    operation = self.client.projects_locations_clusters.SetResourceLabels(
        self.messages.SetLabelsRequest(
            name=ProjectLocationCluster(cluster_ref.projectId,
                                        cluster_ref.zone,
                                        cluster_ref.clusterId),
            resourceLabels=labels,
            labelFingerprint=fingerprint))
    return self.ParseOperation(operation.name, cluster_ref.zone)


class V1Alpha1Adapter(V1Beta1Adapter):
  """APIAdapter for v1alpha1."""

  def CreateCluster(self, cluster_ref, options):
    cluster = self.CreateClusterCommon(cluster_ref, options)
    if options.enable_autoprovisioning is not None:
      cluster.autoscaling = self.CreateClusterAutoscalingCommon(options)
    if options.local_ssd_volume_configs:
      for pool in cluster.nodePools:
        self._AddLocalSSDVolumeConfigsToNodeConfig(pool.config, options)
    req = self.messages.CreateClusterRequest(
        parent=ProjectLocation(cluster_ref.projectId, cluster_ref.zone),
        cluster=cluster)
    operation = self.client.projects_locations_clusters.Create(req)
    return self.ParseOperation(operation.name, cluster_ref.zone)

  def CreateClusterAutoscalingCommon(self, options):
    """Create cluster's autoscaling configuration.

    Args:
      options: Either CreateClusterOptions or UpdateClusterOptions.
    Returns:
      Cluster's autoscaling configuration.
    """
    if (options.enable_autoprovisioning and
        (options.max_cpu is None or options.max_memory is None)):
      raise util.Error(NO_AUTOPROVISIONING_LIMITS_ERROR_MSG)

    resource_limits = []
    if options.min_cpu is not None or options.max_cpu is not None:
      resource_limits.append(self.messages.ResourceLimit(
          name='cpu',
          minimum=options.min_cpu,
          maximum=options.max_cpu))
    if options.min_memory is not None or options.max_memory is not None:
      resource_limits.append(self.messages.ResourceLimit(
          name='memory',
          minimum=options.min_memory,
          maximum=options.max_memory))
    if options.max_accelerator is not None:
      accelerator_type = options.max_accelerator.get('type')
      min_count = 0
      if options.min_accelerator is not None:
        if options.min_accelerator.get('type') != accelerator_type:
          raise util.Error(MISMATCH_ACCELERATOR_TYPE_LIMITS_ERROR_MSG)
        min_count = options.min_accelerator.get('count', 0)
      resource_limits.append(self.messages.ResourceLimit(
          name=options.max_accelerator.get('type'),
          minimum=min_count,
          maximum=options.max_accelerator.get('count', 0)))
    return self.messages.ClusterAutoscaling(
        enableNodeAutoprovisioning=options.enable_autoprovisioning,
        resourceLimits=resource_limits)

  def CreateNodePool(self, node_pool_ref, options):
    pool = self.CreateNodePoolCommon(node_pool_ref, options)
    if options.local_ssd_volume_configs:
      self._AddLocalSSDVolumeConfigsToNodeConfig(pool.config, options)
    if options.enable_autoprovisioning is not None:
      pool.autoscaling.autoprovisioned = options.enable_autoprovisioning
    req = self.messages.CreateNodePoolRequest(
        nodePool=pool,
        parent=ProjectLocationCluster(node_pool_ref.projectId,
                                      node_pool_ref.zone,
                                      node_pool_ref.clusterId))
    operation = self.client.projects_locations_clusters_nodePools.Create(req)
    return self.ParseOperation(operation.name, node_pool_ref.zone)

  def UpdateNodePool(self, node_pool_ref, options):
    if options.IsAutoscalingUpdate():
      autoscaling = self.UpdateNodePoolAutoscaling(node_pool_ref, options)
      update = self.messages.ClusterUpdate(
          desiredNodePoolId=node_pool_ref.nodePoolId,
          desiredNodePoolAutoscaling=autoscaling)
      operation = self.client.projects_locations_clusters.Update(
          self.messages.UpdateClusterRequest(
              name=ProjectLocationCluster(node_pool_ref.projectId,
                                          node_pool_ref.zone,
                                          node_pool_ref.clusterId),
              update=update))
      return self.ParseOperation(operation.name, node_pool_ref.zone)
    else:
      management = self.UpdateNodePoolNodeManagement(node_pool_ref, options)
      req = (self.messages.SetNodePoolManagementRequest(
          name=ProjectLocationClusterNodePool(
              node_pool_ref.projectId,
              node_pool_ref.zone,
              node_pool_ref.clusterId,
              node_pool_ref.nodePoolId),
          management=management))
      operation = (
          self.client.projects_locations_clusters_nodePools.SetManagement(req))
      return self.ParseOperation(operation.name, node_pool_ref.zone)


def _AddNodeLabelsToNodeConfig(node_config, options):
  if options.node_labels is None:
    return
  labels = node_config.LabelsValue()
  props = []
  for key, value in options.node_labels.iteritems():
    props.append(labels.AdditionalProperty(key=key, value=value))
  labels.additionalProperties = props
  node_config.labels = labels


def _AddWorkloadMetadataToNodeConfig(node_config, options, messages):
  if options.workload_metadata_from_node:
    option = options.workload_metadata_from_node
    if option == UNSPECIFIED:
      node_config.workloadMetadataConfig = messages.WorkloadMetadataConfig(
          nodeMetadata=messages.WorkloadMetadataConfig.
          NodeMetadataValueValuesEnum.UNSPECIFIED)
    elif option == SECURE:
      node_config.workloadMetadataConfig = messages.WorkloadMetadataConfig(
          nodeMetadata=messages.WorkloadMetadataConfig.
          NodeMetadataValueValuesEnum.SECURE)
    elif option == EXPOSE:
      node_config.workloadMetadataConfig = messages.WorkloadMetadataConfig(
          nodeMetadata=messages.WorkloadMetadataConfig.
          NodeMetadataValueValuesEnum.EXPOSE)
    else:
      raise util.Error(
          UNKNOWN_WORKLOAD_METADATA_FROM_NODE_ERROR_MSG.format(option=option))


def ProjectLocation(project, location):
  return 'projects/' + project + '/locations/' + location


def ProjectLocationCluster(project, location, cluster):
  return ProjectLocation(project, location) + '/clusters/' + cluster


def ProjectLocationClusterNodePool(project, location, cluster, nodepool):
  return (ProjectLocationCluster(project, location, cluster) +
          '/nodePools/' + nodepool)


def ProjectLocationOperation(project, location, operation):
  return ProjectLocation(project, location) + '/operations/' + operation
