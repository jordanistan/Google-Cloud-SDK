# Copyright 2014 Google Inc. All Rights Reserved.
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
"""Default value constants exposed by core utilities."""

from __future__ import absolute_import
from __future__ import division


DEFAULT_REGISTRY = 'gcr.io'
REGIONAL_REGISTRIES = ['us.gcr.io', 'eu.gcr.io', 'asia.gcr.io']
LAUNCHER_REGISTRIES = ['l.gcr.io', 'launcher.gcr.io']
LAUNCHER_PROJECT = 'cloud-marketplace'
KUBERNETES_REGISTRIES = ['staging-k8s.gcr.io', 'k8s.gcr.io']
# GCR's regional demand-based mirrors of DockerHub.
# These are intended for use with the daemon flag, e.g.
#  --registry-mirror=https://mirror.gcr.io
MIRROR_REGISTRIES = [
    'us-mirror.gcr.io', 'eu-mirror.gcr.io', 'asia-mirror.gcr.io',
    'mirror.gcr.io'
]
MIRROR_PROJECT = 'cloud-containers-mirror'
DEFAULT_REGISTRIES_TO_AUTHENTICATE = (
    [DEFAULT_REGISTRY] + REGIONAL_REGISTRIES + KUBERNETES_REGISTRIES)
ALL_SUPPORTED_REGISTRIES = (
    DEFAULT_REGISTRIES_TO_AUTHENTICATE + LAUNCHER_REGISTRIES +
    MIRROR_REGISTRIES)
DEFAULT_DEVSHELL_IMAGE = (DEFAULT_REGISTRY + '/dev_con/cloud-dev-common:prod')
METADATA_IMAGE = DEFAULT_REGISTRY + '/google_appengine/faux-metadata:latest'
