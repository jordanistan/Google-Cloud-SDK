# Copyright 2018 Google Inc. All Rights Reserved.
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
"""`gcloud access-context-manager policies update` command."""
from googlecloudsdk.api_lib.accesscontextmanager import policies as policies_api
from googlecloudsdk.calliope import base
from googlecloudsdk.command_lib.accesscontextmanager import common
from googlecloudsdk.command_lib.accesscontextmanager import policies


class Update(base.UpdateCommand):
  """Update an existing access policy."""

  @staticmethod
  def Args(parser):
    policies.AddResourceArg(parser, 'to update')
    common.GetTitleArg('access policy').AddToParser(parser)

  def Run(self, args):
    client = policies_api.Client()

    policy_ref = args.CONCEPTS.policy.Parse()

    return client.Patch(policy_ref, title=args.title)
