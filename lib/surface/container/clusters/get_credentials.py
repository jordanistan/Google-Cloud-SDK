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

"""Fetch cluster credentials."""

from googlecloudsdk.api_lib.container import util
from googlecloudsdk.calliope import actions
from googlecloudsdk.calliope import base
from googlecloudsdk.command_lib.container import messages
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core.console import console_io


NOT_RUNNING_MSG = '''\
cluster {0} is not running. The kubernetes API may not be available.'''


class GetCredentials(base.Command):
  """Fetch credentials for a running cluster.

  {command} updates a kubeconfig file with appropriate credentials and endpoint
  information to point kubectl at a specific cluster in Google Kubernetes
  Engine. It takes a project and a zone as parameters, passed through by set
  defaults or flags.
  By default, credentials are written to HOME/.kube/config. You can provide an
  alternate path by setting the KUBECONFIG environment variable.

  This command enables switching to a specific cluster, when working
  with multiple clusters. It can also be used to access a previously created
  cluster from a new workstation.

  See [](https://cloud.google.com/kubernetes-engine/docs/kubectl) for
  kubectl documentation.
  """
  detailed_help = {
      'EXAMPLES':
          """\
          To switch to working on your cluster 'testcluster1', run:

            $ {command} testcluster1 --zone us-central1-f
      """,
  }

  @staticmethod
  def Args(parser):
    """Register flags for this command.

    Args:
      parser: An argparse.ArgumentParser-like object. It is mocked out in order
          to capture some information, but behaves like an ArgumentParser.
    """
    parser.add_argument(
        'name',
        help='The name of the cluster to get credentials for.',
        action=actions.StoreProperty(properties.VALUES.container.cluster))

  def Run(self, args):
    """This is what gets called when the user runs this command.

    Args:
      args: an argparse namespace. All the arguments that were provided to this
        command invocation.

    Raises:
      util.Error: if the cluster is unreachable or not running.
    """
    util.CheckKubectlInstalled()
    adapter = self.context['api_adapter']
    location_get = self.context['location_get']
    location = location_get(args)
    cluster_ref = adapter.ParseCluster(args.name, location)
    if getattr(args, 'region', None):
      message = messages.NonGAFeatureUsingV1APIWarning(self._release_track)
      if message:
        console_io.PromptContinue(message=message, cancel_on_no=True)
    log.status.Print('Fetching cluster endpoint and auth data.')
    # Call DescribeCluster to get auth info and cache for next time
    cluster = adapter.GetCluster(cluster_ref)
    auth = cluster.mainAuth
    # TODO(b/70856999) Make this consistent with the checks in
    # api_lib/container/kubeconfig.py.
    has_creds = (auth and ((auth.clientCertificate and auth.clientKey) or
                           (auth.username and auth.password)))
    if not has_creds and not util.ClusterConfig.UseGCPAuthProvider(cluster):
      raise util.Error(
          'get-credentials requires edit permission on {0}'.format(
              cluster_ref.projectId))
    if not adapter.IsRunning(cluster):
      log.warn(NOT_RUNNING_MSG.format(cluster_ref.clusterId))
    util.ClusterConfig.Persist(cluster, cluster_ref.projectId)
