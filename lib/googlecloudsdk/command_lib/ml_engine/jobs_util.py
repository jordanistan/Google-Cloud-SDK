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
"""ml-engine jobs command code."""
from apitools.base.py import exceptions

from googlecloudsdk.api_lib.ml_engine import jobs
from googlecloudsdk.command_lib.logs import stream
from googlecloudsdk.command_lib.ml_engine import flags
from googlecloudsdk.command_lib.ml_engine import jobs_prep
from googlecloudsdk.command_lib.ml_engine import log_utils
from googlecloudsdk.command_lib.util.apis import arg_utils
from googlecloudsdk.command_lib.util.args import labels_util
from googlecloudsdk.core import execution_utils
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core import resources
from googlecloudsdk.core.resource import resource_printer


_CONSOLE_URL = ('https://console.cloud.google.com/ml/jobs/{job_id}?'
                'project={project}')
_LOGS_URL = ('https://console.cloud.google.com/logs?'
             'resource=ml.googleapis.com%2Fjob_id%2F{job_id}'
             '&project={project}')
JOB_FORMAT = 'yaml(jobId,state,startTime.date(tz=LOCAL),endTime.date(tz=LOCAL))'
# Check every 10 seconds if the job is complete (if we didn't fetch any logs the
# last time)
_CONTINUE_INTERVAL = 10


_TF_RECORD_URL = ('https://www.tensorflow.org/versions/r0.12/how_tos/'
                  'reading_data/index.html#file-formats')


_PREDICTION_DATA_FORMAT_MAPPER = arg_utils.ChoiceEnumMapper(
    '--data-format',
    jobs.GetMessagesModule(
    ).GoogleCloudMlV1PredictionInput.DataFormatValueValuesEnum,
    custom_mappings={
        'TEXT': ('text', ('Text files with instances separated '
                          'by the new-line character.')),
        'TF_RECORD': ('tf-record',
                      'TFRecord files; see {}'.format(_TF_RECORD_URL)),
        'TF_RECORD_GZIP': ('tf-record-gzip',
                           'GZIP-compressed TFRecord files.')
    },
    help_str='Data format of the input files.',
    required=True)

_ACCELERATOR_MAP = arg_utils.ChoiceEnumMapper(
    '--accelerator-type',
    jobs.GetMessagesModule(
    ).GoogleCloudMlV1AcceleratorConfig.TypeValueValuesEnum,
    custom_mappings={
        'NVIDIA_TESLA_K80': ('nvidia-tesla-k80', 'NVIDIA Tesla K80 GPU'),
        'NVIDIA_TESLA_P100': ('nvidia-tesla-p100', 'NVIDIA Tesla P100 GPU.')
    },
    help_str='The available types of accelerators.',
    required=True)

_SCALE_TIER_CHOICES = {
    'BASIC': ('basic', ('A single worker instance. This tier is suitable for '
                        'learning how to use Cloud ML Engine, and for '
                        'experimenting with new models using small datasets.')),
    'STANDARD_1': ('standard-1', 'Many workers and a few parameter servers.'),
    'PREMIUM_1': ('premium-1',
                  'A large number of workers with many parameter servers.'),
    'BASIC_GPU': ('basic-gpu', 'A single worker instance with a GPU.'),
    'BASIC_TPU': ('basic-tpu', 'A single worker instance with a Cloud TPU.'),
    'CUSTOM': ('custom', """\
The CUSTOM tier is not a set tier, but rather enables you to use your own
cluster specification. When you use this tier, set values to configure your
processing cluster according to these guidelines (using the --config flag):

* You _must_ set `TrainingInput.mainType` to specify the type of machine to
  use for your main node. This is the only required setting.
* You _may_ set `TrainingInput.workerCount` to specify the number of workers to
  use. If you specify one or more workers, you _must_ also set
  `TrainingInput.workerType` to specify the type of machine to use for your
  worker nodes.
* You _may_ set `TrainingInput.parameterServerCount` to specify the number of
  parameter servers to use. If you specify one or more parameter servers, you
  _must_ also set `TrainingInput.parameterServerType` to specify the type of
  machine to use for your parameter servers.  Note that all of your workers must
  use the same machine type, which can be different from your parameter server
  type and main type. Your parameter servers must likewise use the same
  machine type, which can be different from your worker type and main type.\
""")
}

_TRAINING_SCALE_TIER_MAPPER = arg_utils.ChoiceEnumMapper(
    '--scale-tier',
    jobs.GetMessagesModule()
    .GoogleCloudMlV1TrainingInput.ScaleTierValueValuesEnum,
    custom_mappings=_SCALE_TIER_CHOICES,
    help_str=('Specifies the machine types, the number of replicas for workers '
              'and parameter servers.'),
    default=None)


def DataFormatFlagMap():
  """Return the ChoiceEnumMapper for the --data-format flag."""
  return _PREDICTION_DATA_FORMAT_MAPPER


def AcceleratorFlagMap():
  """Return the ChoiceEnumMapper for the --accelerator-type flag."""
  return _ACCELERATOR_MAP


def ScaleTierFlagMap():
  """Returns the ChoiceEnumMapper for the --scale-tier flag."""
  return _TRAINING_SCALE_TIER_MAPPER


def _ParseJob(job):
  return resources.REGISTRY.Parse(
      job,
      params={'projectsId': properties.VALUES.core.project.GetOrFail},
      collection='ml.projects.jobs')


def Cancel(jobs_client, job):
  job_ref = _ParseJob(job)
  return jobs_client.Cancel(job_ref)


def PrintDescribeFollowUp(job_id):
  project = properties.VALUES.core.project.Get()
  log.status.Print(
      '\nView job in the Cloud Console at:\n' +
      _CONSOLE_URL.format(job_id=job_id, project=project))
  log.status.Print(
      '\nView logs at:\n' +
      _LOGS_URL.format(job_id=job_id, project=project))


def Describe(jobs_client, job):
  job_ref = _ParseJob(job)
  return jobs_client.Get(job_ref)


def List(jobs_client):
  project_ref = resources.REGISTRY.Parse(
      properties.VALUES.core.project.Get(required=True),
      collection='ml.projects')
  return jobs_client.List(project_ref)


def StreamLogs(job, task_name, polling_interval,
               allow_multiline_logs):
  log_fetcher = stream.LogFetcher(
      filters=log_utils.LogFilters(job, task_name),
      polling_interval=polling_interval, continue_interval=_CONTINUE_INTERVAL,
      continue_func=log_utils.MakeContinueFunction(job))
  return log_utils.SplitMultiline(
      log_fetcher.YieldLogs(), allow_multiline=allow_multiline_logs)


_FOLLOW_UP_MESSAGE = """\
Your job is still active. You may view the status of your job with the command

  $ gcloud ml-engine jobs describe {job_id}

or continue streaming the logs with the command

  $ gcloud ml-engine jobs stream-logs {job_id}\
"""


def PrintSubmitFollowUp(job_id, print_follow_up_message=True):
  log.status.Print('Job [{}] submitted successfully.'.format(job_id))
  if print_follow_up_message:
    log.status.Print(_FOLLOW_UP_MESSAGE.format(job_id=job_id))


def GetStreamLogs(async_, stream_logs):
  """Return, based on the command line arguments, whether we should stream logs.

  Both arguments cannot be set (they're mutually exclusive flags) and the
  default is False.

  Args:
    async_: bool, the value of the --async flag.
    stream_logs: bool, the value of the --stream-logs flag.

  Returns:
    bool, whether to stream the logs

  Raises:
    ValueError: if both async_ and stream_logs are True.
  """
  if async_ and stream_logs:
    # Doesn't have to be a nice error; they're mutually exclusive so we should
    # never get here.
    raise ValueError('--async and --stream-logs cannot both be set.')

  if async_:
    # TODO(b/36195821): Use the flag deprecation machinery when it supports the
    # store_true action
    log.warn('The --async flag is deprecated, as the default behavior is to '
             'submit the job asynchronously; it can be omitted. '
             'For synchronous behavior, please pass --stream-logs.\n')
  return stream_logs


def ParseCreateLabels(jobs_client, args):
  return labels_util.ParseCreateArgs(args, jobs_client.job_class.LabelsValue)


def SubmitTraining(jobs_client, job, job_dir=None, staging_bucket=None,
                   packages=None, package_path=None, scale_tier=None,
                   config=None, module_name=None, runtime_version=None,
                   stream_logs=None, user_args=None, labels=None):
  """Submit a training job."""
  region = properties.VALUES.compute.region.Get(required=True)
  staging_location = jobs_prep.GetStagingLocation(
      staging_bucket=staging_bucket, job_id=job,
      job_dir=job_dir)
  try:
    uris = jobs_prep.UploadPythonPackages(
        packages=packages, package_path=package_path,
        staging_location=staging_location)
  except jobs_prep.NoStagingLocationError:
    raise flags.ArgumentError(
        'If local packages are provided, the `--staging-bucket` or '
        '`--job-dir` flag must be given.')
  log.debug('Using {0} as trainer uris'.format(uris))

  scale_tier_enum = jobs_client.training_input_class.ScaleTierValueValuesEnum
  scale_tier = scale_tier_enum(scale_tier) if scale_tier else None

  job = jobs_client.BuildTrainingJob(
      path=config,
      module_name=module_name,
      job_name=job,
      trainer_uri=uris,
      region=region,
      job_dir=job_dir.ToUrl() if job_dir else None,
      scale_tier=scale_tier,
      user_args=user_args,
      runtime_version=runtime_version,
      labels=labels
  )

  project_ref = resources.REGISTRY.Parse(
      properties.VALUES.core.project.Get(required=True),
      collection='ml.projects')
  job = jobs_client.Create(project_ref, job)
  if not stream_logs:
    PrintSubmitFollowUp(job.jobId, print_follow_up_message=True)
    return job
  else:
    PrintSubmitFollowUp(job.jobId, print_follow_up_message=False)

  log_fetcher = stream.LogFetcher(
      filters=log_utils.LogFilters(job.jobId),
      polling_interval=properties.VALUES.ml_engine.polling_interval.GetInt(),
      continue_interval=_CONTINUE_INTERVAL,
      continue_func=log_utils.MakeContinueFunction(job.jobId))

  printer = resource_printer.Printer(log_utils.LOG_FORMAT,
                                     out=log.err)
  with execution_utils.RaisesKeyboardInterrupt():
    try:
      printer.Print(log_utils.SplitMultiline(log_fetcher.YieldLogs()))
    except KeyboardInterrupt:
      log.status.Print('Received keyboard interrupt.\n')
      log.status.Print(_FOLLOW_UP_MESSAGE.format(job_id=job.jobId,
                                                 project=project_ref.Name()))
    except exceptions.HttpError as err:
      log.status.Print('Polling logs failed:\n{}\n'.format(str(err)))
      log.info('Failure details:', exc_info=True)
      log.status.Print(_FOLLOW_UP_MESSAGE.format(job_id=job.jobId,
                                                 project=project_ref.Name()))

  job_ref = resources.REGISTRY.Parse(
      job.jobId,
      params={'projectsId': properties.VALUES.core.project.GetOrFail},
      collection='ml.projects.jobs')
  job = jobs_client.Get(job_ref)

  return job


def _ValidateSubmitPredictionArgs(model_dir, version):
  if model_dir and version:
    raise flags.ArgumentError('`--version` cannot be set with `--model-dir`')


def SubmitPrediction(jobs_client, job,
                     model_dir=None, model=None, version=None,
                     input_paths=None, data_format=None, output_path=None,
                     region=None, runtime_version=None, max_worker_count=None,
                     batch_size=None, labels=None, accelerator_count=None,
                     accelerator_type=None):
  """Submit a prediction job."""
  _ValidateSubmitPredictionArgs(model_dir, version)

  project_ref = resources.REGISTRY.Parse(
      properties.VALUES.core.project.Get(required=True),
      collection='ml.projects')
  job = jobs_client.BuildBatchPredictionJob(
      job_name=job,
      model_dir=model_dir,
      model_name=model,
      version_name=version,
      input_paths=input_paths,
      data_format=data_format,
      output_path=output_path,
      region=region,
      runtime_version=runtime_version,
      max_worker_count=max_worker_count,
      batch_size=batch_size,
      labels=labels,
      accelerator_count=accelerator_count,
      accelerator_type=_ACCELERATOR_MAP.GetEnumForChoice(accelerator_type)
  )
  PrintSubmitFollowUp(job.jobId, print_follow_up_message=True)
  return jobs_client.Create(project_ref, job)


def GetSummaryFormat(job):
  """Get summary table format for an ml job resource.

  Args:
    job: job resource to build summary output for.

  Returns:
    dynamic format string for resource output.
  """
  if job:
    if getattr(job, 'trainingInput', False):
      if getattr(job.trainingInput, 'hyperparameters', False):
        return flags.GetHPTrainingJobSummary()
      return flags.GetStandardTrainingJobSummary()
    else:
      return flags.GetPredictJobSummary()
  return 'yaml'  # Fallback to yaml on empty resource


def ParseUpdateLabels(client, job_ref, args):
  def GetLabels():
    return client.Get(job_ref).labels
  return labels_util.ProcessUpdateArgsLazy(
      args, client.job_class.LabelsValue, GetLabels)


def Update(jobs_client, operations_client, args):
  job_ref = _ParseJob(args.job)
  labels_update = ParseUpdateLabels(jobs_client, job_ref, args)
  try:
    op = jobs_client.Patch(job_ref, labels_update)
  except jobs.NoFieldsSpecifiedError:
    if not any(args.IsSpecified(arg) for arg in ('update_labels',
                                                 'clear_labels',
                                                 'remove_labels')):
      raise
    log.status.Print('No update to perform.')
    return None
  else:
    return operations_client.WaitForOperation(
        op, message='Updating job [{}]'.format(job_ref.Name())).response
