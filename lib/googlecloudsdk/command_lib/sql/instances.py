# Copyright 2017 Google Inc. All Rights Reserved.
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
"""Common utility functions for sql instance commands."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from googlecloudsdk.api_lib.sql import constants
from googlecloudsdk.api_lib.sql import instance_prop_reducers as reducers
from googlecloudsdk.api_lib.sql import instances as api_util
from googlecloudsdk.calliope import base
from googlecloudsdk.calliope import exceptions
from googlecloudsdk.command_lib.util.args import labels_util

DEFAULT_RELEASE_TRACK = base.ReleaseTrack.GA

# PD = Persistent Disk. This is prefixed to all storage type payloads.
STORAGE_TYPE_PREFIX = 'PD_'


class _BaseInstances(object):
  """Common utility functions for sql instance commands."""

  @classmethod
  def _ConstructBaseSettingsFromArgs(cls,
                                     sql_messages,
                                     args,
                                     instance=None,
                                     release_track=DEFAULT_RELEASE_TRACK):
    """Constructs instance settings from the command line arguments.

    Args:
      sql_messages: module, The messages module that should be used.
      args: argparse.Namespace, The arguments that this command was invoked
          with.
      instance: sql_messages.DatabaseInstance, The original instance, for
          settings that depend on the previous state.
      release_track: base.ReleaseTrack, the release track that this was run
          under.

    Returns:
      A settings object representing the instance settings.

    Raises:
      ToolException: An error other than http error occurred while executing the
          command.
    """
    settings = sql_messages.Settings(
        tier=reducers.MachineType(instance, args.tier, args.memory, args.cpu),
        pricingPlan=args.pricing_plan,
        replicationType=args.replication,
        activationPolicy=args.activation_policy)

    if args.authorized_gae_apps:
      settings.authorizedGaeApplications = args.authorized_gae_apps

    if any([
        args.assign_ip is not None, args.require_ssl is not None,
        args.authorized_networks
    ]):
      settings.ipConfiguration = sql_messages.IpConfiguration()
      if args.assign_ip is not None:
        cls.SetIpConfigurationEnabled(settings, args.assign_ip)

      if args.authorized_networks:
        cls.SetAuthorizedNetworks(settings, args.authorized_networks,
                                  sql_messages.AclEntry)

      if args.require_ssl is not None:
        settings.ipConfiguration.requireSsl = args.require_ssl

    if any([args.follow_gae_app, args.gce_zone]):
      settings.locationPreference = sql_messages.LocationPreference(
          followGaeApplication=args.follow_gae_app, zone=args.gce_zone)

    if args.storage_size:
      settings.dataDiskSizeGb = int(args.storage_size / constants.BYTES_TO_GB)

    if args.storage_auto_increase is not None:
      settings.storageAutoResize = args.storage_auto_increase

    # BETA args.
    if release_track == base.ReleaseTrack.BETA:
      if args.IsSpecified('storage_auto_increase_limit'):
        # Resize limit should be settable if the original instance has resize
        # turned on, or if the instance to be created has resize flag.
        if (instance and instance.settings.storageAutoResize) or (
            args.storage_auto_increase):
          # If the limit is set to None, we want it to be set to 0. This is a
          # backend requirement.
          settings.storageAutoResizeLimit = (args.storage_auto_increase_limit or
                                             0)
        else:
          raise exceptions.RequiredArgumentException(
              '--storage-auto-increase', 'To set the storage capacity limit '
              'using [--storage-auto-increase-limit], '
              '[--storage-auto-increase] must be enabled.')

      if args.IsSpecified('availability_type'):
        settings.availabilityType = args.availability_type

    return settings

  @classmethod
  def _ConstructCreateSettingsFromArgs(cls,
                                       sql_messages,
                                       args,
                                       instance=None,
                                       release_track=DEFAULT_RELEASE_TRACK):
    """Constructs create settings object from base settings and args."""
    original_settings = instance.settings if instance else None
    settings = cls._ConstructBaseSettingsFromArgs(sql_messages, args, instance,
                                                  release_track)

    if args.on_premises_host_port:
      if args.require_ssl:
        raise exceptions.ToolException('Argument --on-premises-host-port not '
                                       'allowed with --require_ssl')
      settings.onPremisesConfiguration = sql_messages.OnPremisesConfiguration(
          hostPort=args.on_premises_host_port)

    backup_configuration = (reducers.BackupConfiguration(
        sql_messages,
        instance,
        backup=args.backup,
        backup_start_time=args.backup_start_time,
        enable_bin_log=args.enable_bin_log))
    if backup_configuration:
      cls.AddBackupConfigToSettings(settings, backup_configuration)

    settings.databaseFlags = (reducers.DatabaseFlags(
        sql_messages, original_settings, database_flags=args.database_flags))

    settings.maintenanceWindow = (reducers.MaintenanceWindow(
        sql_messages,
        instance,
        maintenance_release_channel=args.maintenance_release_channel,
        maintenance_window_day=args.maintenance_window_day,
        maintenance_window_hour=args.maintenance_window_hour))

    if args.storage_type:
      settings.dataDiskType = STORAGE_TYPE_PREFIX + args.storage_type

    # BETA args.
    if release_track == base.ReleaseTrack.BETA:
      settings.userLabels = labels_util.ParseCreateArgs(
          args, sql_messages.Settings.UserLabelsValue)

      # Check that availability type is only specified if this is Postgres.
      if (args.IsSpecified('availability_type') and
          not api_util.InstancesV1Beta4.IsPostgresDatabaseVersion(
              args.database_version)):
        raise exceptions.InvalidArgumentException(
            '--availability-type', 'Cannot set [--availability-type] on a '
            'non-Postgres instance.')

    return settings

  @classmethod
  def _ConstructPatchSettingsFromArgs(cls,
                                      sql_messages,
                                      args,
                                      instance,
                                      release_track=DEFAULT_RELEASE_TRACK):
    """Constructs create settings object from base settings and args."""
    original_settings = instance.settings
    settings = cls._ConstructBaseSettingsFromArgs(sql_messages, args, instance,
                                                  release_track)

    if args.clear_gae_apps:
      settings.authorizedGaeApplications = []

    if any([args.follow_gae_app, args.gce_zone]):
      settings.locationPreference = sql_messages.LocationPreference(
          followGaeApplication=args.follow_gae_app, zone=args.gce_zone)

    if args.clear_authorized_networks:
      if not settings.ipConfiguration:
        settings.ipConfiguration = sql_messages.IpConfiguration()
      settings.ipConfiguration.authorizedNetworks = []

    if args.enable_database_replication is not None:
      settings.databaseReplicationEnabled = args.enable_database_replication

    backup_configuration = (reducers.BackupConfiguration(
        sql_messages,
        instance,
        no_backup=args.no_backup,
        backup_start_time=args.backup_start_time,
        enable_bin_log=args.enable_bin_log))
    if backup_configuration:
      cls.AddBackupConfigToSettings(settings, backup_configuration)

    settings.databaseFlags = (reducers.DatabaseFlags(
        sql_messages,
        original_settings,
        database_flags=args.database_flags,
        clear_database_flags=args.clear_database_flags))

    settings.maintenanceWindow = (reducers.MaintenanceWindow(
        sql_messages,
        instance,
        maintenance_release_channel=args.maintenance_release_channel,
        maintenance_window_day=args.maintenance_window_day,
        maintenance_window_hour=args.maintenance_window_hour))

    # BETA args.
    if release_track == base.ReleaseTrack.BETA:
      labels_diff = labels_util.ExplicitNullificationDiff.FromUpdateArgs(args)
      labels_update = labels_diff.Apply(
          sql_messages.Settings.UserLabelsValue, instance.settings.userLabels)
      if labels_update.needs_update:
        settings.userLabels = labels_update.labels

    return settings

  @classmethod
  def _ConstructBaseInstanceFromArgs(cls,
                                     sql_messages,
                                     args,
                                     original=None,
                                     instance_ref=None,
                                     release_track=DEFAULT_RELEASE_TRACK):
    """Construct a Cloud SQL instance from command line args.

    Args:
      sql_messages: module, The messages module that should be used.
      args: argparse.Namespace, The CLI arg namespace.
      original: sql_messages.DatabaseInstance, The original instance, if some of
          it might be used to fill fields in the new one.
      instance_ref: reference to DatabaseInstance object, used to fill project
          and instance information.
      release_track: base.ReleaseTrack, the release track that this was run
          under.

    Returns:
      sql_messages.DatabaseInstance, The constructed (and possibly partial)
      database instance.

    Raises:
      ToolException: An error other than http error occurred while executing the
          command.
    """
    del args, original, release_track  # Currently unused in base function.
    instance_resource = sql_messages.DatabaseInstance()

    if instance_ref:
      cls.SetProjectAndInstanceFromRef(instance_resource, instance_ref)

    return instance_resource

  @classmethod
  def ConstructCreateInstanceFromArgs(cls,
                                      sql_messages,
                                      args,
                                      original=None,
                                      instance_ref=None,
                                      release_track=DEFAULT_RELEASE_TRACK):
    """Constructs Instance for create request from base instance and args."""
    instance_resource = cls._ConstructBaseInstanceFromArgs(
        sql_messages, args, original, instance_ref)

    instance_resource.region = args.region
    instance_resource.databaseVersion = args.database_version
    instance_resource.mainInstanceName = args.main_instance_name

    instance_resource.settings = cls._ConstructCreateSettingsFromArgs(
        sql_messages, args, original, release_track)

    if args.main_instance_name:
      replication = 'ASYNCHRONOUS'
      if args.replica_type == 'FAILOVER':
        instance_resource.replicaConfiguration = (
            sql_messages.ReplicaConfiguration(failoverTarget=True))
    else:
      replication = 'SYNCHRONOUS'
    if not args.replication:
      instance_resource.settings.replicationType = replication

    if args.failover_replica_name:
      instance_resource.failoverReplica = (
          sql_messages.DatabaseInstance.FailoverReplicaValue(
              name=args.failover_replica_name))

    return instance_resource

  @classmethod
  def ConstructPatchInstanceFromArgs(cls,
                                     sql_messages,
                                     args,
                                     original,
                                     instance_ref=None,
                                     release_track=DEFAULT_RELEASE_TRACK):
    """Constructs Instance for patch request from base instance and args."""
    instance_resource = cls._ConstructBaseInstanceFromArgs(
        sql_messages, args, original, instance_ref)

    instance_resource.settings = cls._ConstructPatchSettingsFromArgs(
        sql_messages, args, original, release_track)

    return instance_resource


class InstancesV1Beta3(_BaseInstances):
  """Common utility functions for sql instances V1Beta3."""

  @staticmethod
  def SetProjectAndInstanceFromRef(instance_resource, instance_ref):
    instance_resource.project = instance_ref.project
    instance_resource.instance = instance_ref.instance

  @staticmethod
  def AddBackupConfigToSettings(settings, backup_config):
    settings.backupConfiguration = [backup_config]

  @staticmethod
  def SetIpConfigurationEnabled(settings, assign_ip):
    settings.ipConfiguration.enabled = assign_ip

  @staticmethod
  def SetAuthorizedNetworks(settings, authorized_networks, acl_entry_value):
    del acl_entry_value  # Unused in v1beta3
    settings.ipConfiguration.authorizedNetworks = authorized_networks


class InstancesV1Beta4(_BaseInstances):
  """Common utility functions for sql instances V1Beta4."""

  @staticmethod
  def SetProjectAndInstanceFromRef(instance_resource, instance_ref):
    instance_resource.project = instance_ref.project
    instance_resource.name = instance_ref.instance

  @staticmethod
  def AddBackupConfigToSettings(settings, backup_config):
    settings.backupConfiguration = backup_config

  @staticmethod
  def SetIpConfigurationEnabled(settings, assign_ip):
    settings.ipConfiguration.ipv4Enabled = assign_ip

  @staticmethod
  def SetAuthorizedNetworks(settings, authorized_networks, acl_entry_value):
    settings.ipConfiguration.authorizedNetworks = [
        acl_entry_value(value=n) for n in authorized_networks
    ]
