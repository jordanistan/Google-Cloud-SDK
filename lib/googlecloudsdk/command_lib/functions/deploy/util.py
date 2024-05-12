# Copyright 2016 Google Inc. All Rights Reserved.
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
"""'functions deploy' utilities."""
import os
import random
import re
import string

from apitools.base.py import http_wrapper
from apitools.base.py import transfer


from googlecloudsdk.api_lib.functions import exceptions
from googlecloudsdk.api_lib.functions import util
from googlecloudsdk.api_lib.storage import storage_util
from googlecloudsdk.calliope import exceptions as calliope_exceptions
from googlecloudsdk.command_lib.util import gcloudignore
from googlecloudsdk.core import exceptions as core_exceptions
from googlecloudsdk.core import http as http_utils
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core import resources
from googlecloudsdk.core.util import archive
from googlecloudsdk.core.util import files as file_utils


TRIGGER_BUCKET_NOTICE = (
    'The default trigger_event for `--trigger-bucket` will soon '
    'change to `object.finalize` from `object.change`. '
    'To opt-in to the new behavior early, run'
    '`gcloud config set functions/use_new_object_trigger True`. To restore old '
    'behavior you can run '
    '`gcloud config set functions/use_new_object_trigger False` '
    'or use the `--trigger-event` flag e.g. '
    '`gcloud functions deploy --trigger-event '
    'providers/cloud.storage/eventTypes/object.change '
    '--trigger-resource gs://...`'
    'Please see https://cloud.google.com/storage/docs/pubsub-notifications'
    'for more information on storage event types.')


def _CleanOldSourceInfo(function):
  function.sourceArchiveUrl = None
  function.sourceRepository = None
  function.sourceUploadUrl = None


def _GcloudIgnoreCreationPredicate(directory):
  return gcloudignore.AnyFileOrDirExists(
      directory, gcloudignore.GIT_FILES + ['node_modules'])


def _GetChooser(path):
  default_ignore_file = gcloudignore.DEFAULT_IGNORE_FILE + '\nnode_modules\n'

  return gcloudignore.GetFileChooserForDir(
      path, default_ignore_file=default_ignore_file,
      gcloud_ignore_creation_predicate=_GcloudIgnoreCreationPredicate)


def _ValidateUnpackedSourceSize(path):
  chooser = _GetChooser(path)
  predicate = chooser.IsIncluded
  size_b = file_utils.GetTreeSizeBytes(path, predicate=predicate)
  size_limit_mb = 512
  size_limit_b = size_limit_mb * 2 ** 20
  if size_b > size_limit_b:
    raise exceptions.OversizedDeployment(
        str(size_b) + 'B', str(size_limit_b) + 'B')


def _CreateSourcesZipFile(zip_dir, source_path):
  """Prepare zip file with source of the function to upload.

  Args:
    zip_dir: str, directory in which zip file will be located. Name of the file
             will be `fun.zip`.
    source_path: str, directory containing the sources to be zipped.
  Returns:
    Path to the zip file (str).
  Raises:
    FunctionsError
  """
  util.ValidateDirectoryExistsOrRaiseFunctionError(source_path)
  _ValidateUnpackedSourceSize(source_path)
  zip_file_name = os.path.join(zip_dir, 'fun.zip')
  try:
    chooser = _GetChooser(source_path)
    predicate = chooser.IsIncluded
    archive.MakeZipFromDir(zip_file_name, source_path, predicate=predicate)
  except ValueError as e:
    raise exceptions.FunctionsError(
        'Error creating a ZIP archive with the source code '
        'for directory {0}: {1}'.format(source_path, str(e)))
  return zip_file_name


def _GenerateRemoteZipFileName(function_name):
  suffix = ''.join(random.choice(string.ascii_lowercase) for _ in range(12))
  return '{0}-{1}-{2}.zip'.format(
      properties.VALUES.functions.region.Get(), function_name, suffix)


def _UploadFileToGcs(source, function_ref, stage_bucket):
  """Upload local source files to GCS staging bucket."""
  zip_file = _GenerateRemoteZipFileName(function_ref.RelativeName())
  bucket_ref = storage_util.BucketReference.FromArgument(
      stage_bucket)
  gcs_url = storage_util.ObjectReference(bucket_ref, zip_file).ToUrl()
  upload_result = storage_util.RunGsutilCommand(
      'cp', '{local} {remote}'.format(local=source, remote=gcs_url))
  if upload_result != 0:
    raise exceptions.FunctionsError(
        'Failed to upload the function source code to the bucket {0}'
        .format(stage_bucket))
  return gcs_url


def CleanOldTriggerInfo(function, update_mask):
  function.eventTrigger = None
  function.httpsTrigger = None
  update_mask.extend(['eventTrigger', 'httpsTrigger'])


def _AddDefaultBranch(source_archive_url):
  cloud_repo_pattern = (r'^https://source\.developers\.google\.com'
                        r'/projects/[^/]+'
                        r'/repos/[^/]+$')
  if re.match(cloud_repo_pattern, source_archive_url):
    return source_archive_url + '/moveable-aliases/main'
  return source_archive_url


def _GetUploadUrl(messages, service, function_ref):
  request = (messages.
             CloudfunctionsProjectsLocationsFunctionsGenerateUploadUrlRequest)(
                 parent='projects/{}/locations/{}'.format(
                     function_ref.projectsId, function_ref.locationsId))
  response = service.GenerateUploadUrl(request)
  return response.uploadUrl


def _CheckUploadStatus(status_code):
  """Validates that HTTP status for upload is 2xx."""
  return status_code / 100 == 2


def _UploadFileToGeneratedUrl(source, messages, service, function_ref):
  """Upload function source to URL generated by API."""
  url = _GetUploadUrl(messages, service, function_ref)
  upload = transfer.Upload.FromFile(source,
                                    mime_type='application/zip')
  upload_request = http_wrapper.Request(
      url, http_method='PUT', headers={
          'content-type': 'application/zip',
          # Magic header, request will fail without it.
          # Not documented at the moment this comment was being written.
          'x-goog-content-length-range': '0,104857600',
          'Content-Length': '{0:d}'.format(upload.total_size)})
  upload_request.body = upload.stream.read()
  response = http_wrapper.MakeRequest(
      http_utils.Http(), upload_request, retry_func=upload.retry_func,
      retries=upload.num_retries)
  if not _CheckUploadStatus(response.status_code):
    raise exceptions.FunctionsError(
        'Failed to upload the function source code to signed url: {url}. '
        'Status: [{code}:{detail}]'.format(url=url,
                                           code=response.status_code,
                                           detail=response.content))
  return url


def UploadFile(source, stage_bucket, messages, service, function_ref):
  if stage_bucket:
    return _UploadFileToGcs(source, function_ref, stage_bucket)
  return _UploadFileToGeneratedUrl(source, messages, service, function_ref)


def AddSourceToFunction(function, function_ref, update_mask, source_arg,
                        stage_bucket, messages, service):
  """Add sources to function."""
  _CleanOldSourceInfo(function)
  if source_arg is None:
    source_arg = '.'
  source_arg = source_arg or '.'
  if source_arg.startswith('gs://'):
    update_mask.append('sourceArchiveUrl')
    function.sourceArchiveUrl = source_arg
    return
  if source_arg.startswith('https://'):
    update_mask.append('sourceRepository')
    function.sourceRepository = messages.SourceRepository(
        url=_AddDefaultBranch(source_arg)
    )
    return
  with file_utils.TemporaryDirectory() as tmp_dir:
    zip_file = _CreateSourcesZipFile(tmp_dir, source_arg)
    upload_url = UploadFile(
        zip_file, stage_bucket, messages, service, function_ref)
    if upload_url.startswith('gs://'):
      update_mask.append('sourceArchiveUrl')
      function.sourceArchiveUrl = upload_url
    else:
      update_mask.append('sourceUploadUrl')
      function.sourceUploadUrl = upload_url


def ConvertTriggerArgsToRelativeName(trigger_provider, trigger_event,
                                     trigger_resource):
  """Prepares resource field for Function EventTrigger to use in API call.

  API uses relative resource name in EventTrigger message field. The
  structure of that identifier depends on the resource type which depends on
  combination of --trigger-provider and --trigger-event arguments' values.
  This function chooses the appropriate form, fills it with required data and
  returns as a string.

  Args:
    trigger_provider: The --trigger-provider flag value.
    trigger_event: The --trigger-event flag value.
    trigger_resource: The --trigger-resource flag value.
  Returns:
    Relative resource name to use in EventTrigger field.
  """
  resource_type = util.input_trigger_provider_registry.Event(
      trigger_provider, trigger_event).resource_type
  params = {}
  if resource_type.value.collection_id == 'cloudresourcemanager.projects':
    params['projectId'] = properties.VALUES.core.project.GetOrFail
  elif resource_type.value.collection_id == 'pubsub.projects.topics':
    params['projectsId'] = properties.VALUES.core.project.GetOrFail
  elif resource_type.value.collection_id == 'cloudfunctions.projects.buckets':
    pass

  ref = resources.REGISTRY.Parse(
      trigger_resource,
      params,
      collection=resource_type.value.collection_id,
  )
  return ref.RelativeName()


def DeduceAndCheckArgs(args):
  """Check command arguments and deduce information if possible.

  0. Check if --source-revision, --source-branch or --source-tag are present
     when --source-url is not present. (and fail if it is so)
  1. Check if --source-bucket is present when --source-url is present.
  2. Validate if local-path is a directory.
  3. Check if --source-path is present when --source-url is present.
  4. Check if --trigger-event, --trigger-resource or --trigger-path are
     present when --trigger-provider is not present. (and fail if it is so)
  5. Check --trigger-* family of flags deducing default values if possible and
     necessary.

  Args:
    args: The argument namespace.

  Returns:
    None, when using HTTPS trigger. Otherwise a dictionary containing
    trigger_provider, trigger_event, and trigger_resource.
  """
  # This function should raise ArgumentParsingError, but:
  # 1. ArgumentParsingError requires the  argument returned from add_argument)
  #    and Args() method is static. So there is no elegant way to save it
  #    to be reused here.
  # 2. _CheckArgs() is invoked from Run() and ArgumentParsingError thrown
  #    from Run are not caught.
  _ValidateTriggerArgs(args)
  return _CheckTriggerEventArgs(args)


def _ValidateTriggerArgs(args):
  """Check if args related function triggers are valid.

  Args:
    args: parsed command line arguments.
  Raises:
    FunctionsError.
  """
  # checked that Event Type is valid
  trigger_event = args.trigger_event
  trigger_resource = args.trigger_resource
  if trigger_event:
    trigger_provider = util.input_trigger_provider_registry.ProviderForEvent(
        trigger_event).label
    if not trigger_provider:
      raise exceptions.FunctionsError(
          'Unsupported trigger_event {}'.format(trigger_event))

    resource_type = util.input_trigger_provider_registry.Event(
        trigger_provider, trigger_event).resource_type
    if trigger_resource is None and resource_type != util.Resources.PROJECT:
      raise exceptions.FunctionsError(
          'You must provide --trigger-resource when using '
          '--trigger-event={}'.format(trigger_event))

  if args.IsSpecified('retry') and args.IsSpecified('trigger_http'):
    raise calliope_exceptions.ConflictingArgumentsException(
        '--trigger-http', '--retry')


def _BucketTrigger(trigger_bucket):
  bucket_name = trigger_bucket[5:-1]
  new_behavior = properties.VALUES.functions.use_new_object_trigger.GetBool()
  if new_behavior is None:
    log.warn(TRIGGER_BUCKET_NOTICE)

  return {
      'trigger_provider': 'cloud.storage',
      'trigger_event':
          ('google.storage.object.finalize' if new_behavior
           else 'providers/cloud.storage/eventTypes/object.change'),
      'trigger_resource': bucket_name,
  }


def _TopicTrigger(trigger_topic):
  return {
      'trigger_provider': 'cloud.pubsub',
      'trigger_event': 'providers/cloud.pubsub/eventTypes/topic.publish',
      'trigger_resource': trigger_topic,
  }


def _CheckTriggerEventArgs(args):
  """Check --trigger-*  arguments and deduce if possible.

  0. if --trigger-http is return None.
  1. if --trigger-bucket return bucket trigger args (_BucketTrigger)
  2. if --trigger-topic return pub-sub trigger args (_TopicTrigger)
  3. if --trigger-event, deduce provider and resource from registry and return

  Args:
    args: The argument namespace.

  Returns:
    None, when using HTTPS trigger. Otherwise a dictionary containing
    trigger_provider, trigger_event, and trigger_resource.
  """
  if args.trigger_http:
    return None
  if args.trigger_bucket:
    return _BucketTrigger(args.trigger_bucket)
  if args.trigger_topic:
    return _TopicTrigger(args.trigger_topic)
  if not args.trigger_event:
    return None

  trigger_event = args.trigger_event
  trigger_provider = util.input_trigger_provider_registry.ProviderForEvent(
      trigger_event).label
  trigger_resource = args.trigger_resource
  resource_type = util.input_trigger_provider_registry.Event(
      trigger_provider, trigger_event).resource_type
  if resource_type == util.Resources.TOPIC:
    trigger_resource = util.ValidatePubsubTopicNameOrRaise(
        trigger_resource)
  elif resource_type == util.Resources.BUCKET:
    trigger_resource = storage_util.BucketReference.FromBucketUrl(
        trigger_resource).bucket
  elif resource_type == util.Resources.PROJECT:
    if trigger_resource:
      properties.VALUES.core.project.Validate(trigger_resource)
  else:
    # Check if programmer allowed other methods in
    # util.PROVIDER_EVENT_RESOURCE but forgot to update code here
    raise core_exceptions.InternalError()
  # checked if provided resource and path have correct format
  return {
      'trigger_provider': trigger_provider,
      'trigger_event': trigger_event,
      'trigger_resource': trigger_resource,
  }
