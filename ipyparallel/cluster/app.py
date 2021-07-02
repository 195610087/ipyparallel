#!/usr/bin/env python
# encoding: utf-8
"""The ipcluster application."""
from __future__ import print_function

import errno
import json
import logging
import os
import re
import signal
import sys

import zmq
from IPython.core.profiledir import ProfileDir
from traitlets import Bool
from traitlets import CaselessStrEnum
from traitlets import Dict
from traitlets import Integer
from traitlets import List
from traitlets import observe
from traitlets.config.application import catch_config_error

from ipyparallel._version import __version__
from ipyparallel.apps.baseapp import base_aliases
from ipyparallel.apps.baseapp import base_flags
from ipyparallel.apps.baseapp import BaseParallelApplication
from ipyparallel.cluster import Cluster
from ipyparallel.cluster import ClusterManager
from ipyparallel.util import abbreviate_profile_dir


# -----------------------------------------------------------------------------
# Module level variables
# -----------------------------------------------------------------------------

_description = """Start an IPython cluster for parallel computing.

An IPython cluster consists of 1 controller and 1 or more engines.
This command automates the startup of these processes using a wide range of
startup methods (SSH, local processes, PBS, mpiexec, SGE, LSF, HTCondor,
Slurm, Windows HPC Server 2008). To start a cluster with 4 engines on your
local host simply do 'ipcluster start --n=4'. For more complex usage
you will typically do 'ipython profile create mycluster --parallel', then edit
configuration files, followed by 'ipcluster start --profile=mycluster --n=4'.
"""

_main_examples = """
ipcluster start --n=4 # start a 4 node cluster on localhost
ipcluster start -h    # show the help string for the start subcmd

ipcluster stop -h     # show the help string for the stop subcmd
ipcluster engines -h  # show the help string for the engines subcmd
"""

_start_examples = """
ipython profile create mycluster --parallel # create mycluster profile
ipcluster start --profile=mycluster --n=4   # start mycluster with 4 nodes

# args to `ipcluster start` after `--` are passed through to the controller
# see `ipcontroller -h` for options
ipcluster start -- --sqlitedb
"""

_stop_examples = """
ipcluster stop --profile=mycluster  # stop a running cluster by profile name
"""

_engines_examples = """
ipcluster engines --profile=mycluster --n=4  # start 4 engines only
"""


# Exit codes for ipcluster

# This will be the exit code if the ipcluster appears to be running because
# a .pid file exists
ALREADY_STARTED = 10


# This will be the exit code if ipcluster stop is run, but there is not .pid
# file to be found.
ALREADY_STOPPED = 11

# This will be the exit code if ipcluster engines is run, but there is not .pid
# file to be found.
NO_CLUSTER = 12


# -----------------------------------------------------------------------------
# Main application
# -----------------------------------------------------------------------------

start_help = """Start an IPython cluster for parallel computing

Start an ipython cluster by its profile name or cluster
directory. Cluster directories contain configuration, log and
security related files and are named using the convention
'profile_<name>' and should be creating using the 'start'
subcommand of 'ipcluster'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster start --n=4 --profile=<profile>`,
otherwise use the 'profile-dir' option.
"""
stop_help = """Stop a running IPython cluster

Stop a running ipython cluster by its profile name or cluster
directory. Cluster directories are named using the convention
'profile_<name>'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster stop --profile=<profile>`, otherwise
use the '--profile-dir' option.
"""
engines_help = """Start engines connected to an existing IPython cluster

Start one or more engines to connect to an existing Cluster
by profile name or cluster directory.
Cluster directories contain configuration, log and
security related files and are named using the convention
'profile_<name>' and should be creating using the 'start'
subcommand of 'ipcluster'. If your cluster directory is in
the cwd or the ipython directory, you can simply refer to it
using its profile name, 'ipcluster engines --n=4 --profile=<profile>`,
otherwise use the 'profile-dir' option.
"""
stop_aliases = dict(
    signal='IPClusterStop.signal',
)
stop_aliases.update(base_aliases)


class IPClusterStop(BaseParallelApplication):
    name = u'ipcluster'
    description = stop_help
    examples = _stop_examples

    signal = Integer(
        signal.SIGINT, config=True, help="signal to use for stopping processes."
    )

    aliases = Dict(stop_aliases)

    def start(self):
        """Start the app for the stop subcommand."""
        try:
            cluster = Cluster.from_file(
                profile_dir=self.profile_dir.location,
                cluster_id=self.cluster_id,
                parent=self,
            )
        except FileNotFoundError as s:
            self.log.critical(f"Could not find cluster file {s}")
            self.exit(ALREADY_STOPPED)
        # TODO: implement check-if-running!
        cluster.stop_cluster_sync()


list_aliases = {}
list_aliases.update(base_aliases)
list_aliases.update({"o": "IPClusterList.output_format"})


class IPClusterList(BaseParallelApplication):
    name = 'ipcluster'
    description = "List available clusters"
    aliases = list_aliases

    output_format = CaselessStrEnum(
        ["text", "json"], default_value="text", config=True, help="Output format"
    )

    def start(self):
        cluster_manager = ClusterManager(parent=self)
        clusters = cluster_manager.load_clusters()
        if self.output_format == "text":
            # TODO: measure needed profile/cluster id width
            print(
                f"{'PROFILE':16} {'CLUSTER ID':32} {'RUNNING':7} {'ENGINES':7} {'LAUNCHER'}"
            )
            for cluster in sorted(
                clusters.values(),
                key=lambda c: (
                    c.profile_dir,
                    c.cluster_id,
                ),
            ):
                profile = abbreviate_profile_dir(cluster.profile_dir)
                cluster_id = cluster.cluster_id
                running = bool(cluster._controller)
                # TODO: URL?
                engines = 0
                if cluster._engine_sets:
                    engines = sum(
                        engine_set.n for engine_set in cluster._engine_sets.values()
                    )

                launcher = cluster.engine_launcher_class.__name__
                if launcher.endswith("EngineSetLauncher"):
                    launcher = launcher[: -len("EngineSetLauncher")]
                print(
                    f"{profile:16} {cluster_id or repr(''):32} {str(running):7} {engines:7} {launcher}"
                )
        elif self.output_format == "json":
            json.dump(
                [cluster.to_dict() for cluster in clusters.values()],
                sys.stdout,
            )
        else:
            raise NotImplementedError(f"No such output format: {self.output_format}")


engine_aliases = {}
engine_aliases.update(base_aliases)
engine_aliases.update(
    dict(
        n='Cluster.n',
        engines='Cluster.engine_launcher_class',
        daemonize='IPClusterEngines.daemonize',
    )
)
engine_flags = {}
engine_flags.update(base_flags)

engine_flags.update(
    dict(
        daemonize=(
            {'IPClusterEngines': {'daemonize': True}},
            """run the cluster into the background (not available on Windows)""",
        )
    )
)


class IPClusterEngines(BaseParallelApplication):

    name = u'ipcluster'
    description = engines_help
    examples = _engines_examples
    usage = None
    default_log_level = logging.INFO
    classes = List()

    def _classes_default(self):
        from ipyparallel.cluster.launcher import all_launchers

        eslaunchers = [l for l in all_launchers if 'EngineSet' in l.__name__]
        return [ProfileDir, Cluster] + eslaunchers

    daemonize = Bool(
        False,
        config=True,
        help="""Daemonize the ipcluster program. This implies --log-to-file.
        """,
    )

    @observe('daemonize')
    def _daemonize_changed(self, change):
        if change['new']:
            self.log_to_file = True

    early_shutdown = Integer(
        30,
        config=True,
        help="If engines stop in this time frame, assume something is wrong and tear down the cluster.",
    )
    _stopping = False

    aliases = Dict(engine_aliases)
    flags = Dict(engine_flags)

    @catch_config_error
    def initialize(self, argv=None):
        super(IPClusterEngines, self).initialize(argv)
        self.init_signal()
        self.init_cluster()

    def init_deprecated_config(self):
        super().init_deprecated_config()
        cluster_config = self.config.Cluster
        for clsname in ['IPClusterStart', 'IPClusterEngines']:
            if clsname not in self.config:
                continue
            cls_config = self.config[clsname]
            for traitname in [
                'delay',
                'engine_launcher_class',
                'controller_launcher_class',
                'controller_ip',
                'controller_location',
                'n',
            ]:
                if traitname in cls_config and traitname not in cluster_config:
                    value = cls_config[traitname]
                    self.log.warning(
                        f"{clsname}.{traitname} = {value} configuration is deprecated in ipyparallel 7. Use Cluster.{traitname} = {value}"
                    )
                    cluster_config[traitname] = value
                    cls_config.pop(traitname)

    def init_cluster(self):
        self.cluster = Cluster(
            parent=self,
            profile_dir=self.profile_dir.location,
            cluster_id=self.cluster_id,
            controller_args=self.extra_args,
            shutdown_atexit=not self.daemonize,
        )

    def init_signal(self):
        # Setup signals
        signal.signal(signal.SIGINT, self.sigint_handler)

    def engines_started_ok(self):
        self.log.info("Engines appear to have started successfully")
        self.early_shutdown = 0

    async def start_engines(self):
        try:
            await self.cluster.start_engines(self.n)
        except:
            self.log.exception("Engine start failed")
            raise

        if self.daemonize:
            self.loop.add_callback(self.loop.stop)
            return

        self.watch_engines()

    def watch_engines(self):
        """Watch for early engine shutdown"""
        # FIXME: public API to get launcher instances?
        self.engine_launcher = next(iter(self.cluster._engine_sets.values()))

        if not self.early_shutdown:
            self.engine_launcher.on_stop(self.engines_stopped)
            return

        # TODO: enable 'engines stopped early' with new cluster API
        self.engine_launcher.on_stop(self.engines_stopped_early)
        if self.early_shutdown:
            self.loop.add_timeout(
                self.loop.time() + self.early_shutdown, self.engines_started_ok
            )

    def engines_stopped_early(self, stop_data):
        if self.early_shutdown and not self._stopping:
            self.log.error(
                """
            Engines shutdown early, they probably failed to connect.

            Check the engine log files for output.

            If your controller and engines are not on the same machine, you probably
            have to instruct the controller to listen on an interface other than localhost.

            You can set this by adding "--ip=*" to your ControllerLauncher.controller_args.

            Be sure to read our security docs before instructing your controller to listen on
            a public interface.
            """
            )
            self.loop.add_callback(self.stop_cluster)

        return self.engines_stopped(stop_data)

    def engines_stopped(self, r):
        return self.loop.stop()

    async def stop_cluster(self, r=None):
        if not self._stopping:
            self._stopping = True
            self.log.error("IPython cluster: stopping")
            await self.cluster.stop_cluster()
            self.loop.add_callback(self.loop.stop)

    def sigint_handler(self, signum, frame):
        self.log.debug("SIGINT received, stopping launchers...")
        self.loop.add_callback_from_signal(self.stop_cluster)

    def start_logging(self):
        # Remove old log files of the controller and engine
        if self.clean_logs:
            log_dir = self.profile_dir.log_dir
            for f in os.listdir(log_dir):
                if re.match(r'ip(engine|controller)-.+\.(log|err|out)', f):
                    os.remove(os.path.join(log_dir, f))

    def start(self):
        """Start the app for the engines subcommand."""
        self.log.info(f"IPython cluster start: {self.cluster_id}")
        # First see if the cluster is already running

        # Now log and daemonize
        self.log.info('Starting engines with [daemon=%r]' % self.daemonize)

        self.loop.add_callback(self.start_engines)
        # Now write the new pid file AFTER our new forked pid is active.
        # self.write_pid_file()
        try:
            self.loop.start()
        except KeyboardInterrupt:
            pass
        except zmq.ZMQError as e:
            if e.errno == errno.EINTR:
                pass
            else:
                raise


start_aliases = {}
start_aliases.update(engine_aliases)
start_aliases.update(
    dict(
        delay='IPClusterStart.delay',
        controller='IPClusterStart.controller_launcher_class',
        ip='IPClusterStart.controller_ip',
        location='IPClusterStart.controller_location',
    )
)
start_aliases['clean-logs'] = 'IPClusterStart.clean_logs'


class IPClusterStart(IPClusterEngines):

    name = u'ipcluster'
    description = start_help
    examples = _start_examples
    default_log_level = logging.INFO
    auto_create = Bool(
        True, config=True, help="whether to create the profile_dir if it doesn't exist"
    )
    classes = List()

    def _classes_default(
        self,
    ):
        from ipyparallel.cluster.launcher import all_launchers

        return [ProfileDir] + [IPClusterEngines] + all_launchers

    clean_logs = Bool(
        True, config=True, help="whether to cleanup old logs before starting"
    )

    # flags = Dict(flags)
    aliases = Dict(start_aliases)

    def engines_stopped(self, r):
        """prevent parent.engines_stopped from stopping everything on engine shutdown"""
        pass

    async def start_cluster(self):
        await self.cluster.start_cluster()
        if self.daemonize:
            self.log.info(f"Leaving cluster running: {self.cluster.cluster_file}")
            self.loop.add_callback(self.loop.stop)
        self.cluster._controller.on_stop(self.controller_stopped)
        self.watch_engines()

    def controller_stopped(self, stop_data):
        if not self._stopping:
            self.log.warning("Controller stopped. Shutting down.")
            self.loop.add_callback(self.stop_cluster)

    def start(self):
        """Start the app for the start subcommand."""
        # First see if the cluster is already running
        cluster_file = self.cluster.cluster_file
        if os.path.isfile(cluster_file):
            try:
                cluster = Cluster.from_file(cluster_file)
            except Exception as e:
                # TODO: define special ClusterNotRunning exception to handle here
                self.log.error(
                    f"Error loading cluster from file {cluster_file}: {e}. Assuming stopped cluster."
                )
            else:
                self.log.critical(
                    f'Cluster is already running at {self.cluster.cluster_file}. '
                    'use `ipcluster stop` to stop the cluster.'
                )
                # Here I exit with a unusual exit status that other processes
                # can watch for to learn how I existed.
                self.exit(ALREADY_STARTED)

        # Now log and daemonize
        self.log.info('Starting ipcluster with [daemonize=%r]' % self.daemonize)

        self.loop.add_callback(self.start_cluster)
        try:
            self.loop.start()
        except KeyboardInterrupt:
            pass
        except zmq.ZMQError as e:
            if e.errno == errno.EINTR:
                pass
            else:
                raise
        finally:
            if not self.daemonize:
                self.cluster.stop_cluster_sync()


class IPClusterNBExtension(BaseParallelApplication):
    """Enable/disable ipcluster tab extension in Jupyter notebook"""

    name = 'ipcluster-nbextension'

    description = """Enable/disable IPython clusters tab in Jupyter notebook

    for Jupyter Notebook >= 4.2, you can use the new nbextension API:

    jupyter serverextension enable --py ipyparallel
    jupyter nbextension install --py ipyparallel
    jupyter nbextension enable --py ipyparallel
    """

    examples = """
    ipcluster nbextension enable
    ipcluster nbextension disable
    """
    version = __version__
    user = Bool(False, help="Apply the operation only for the given user").tag(
        config=True
    )
    flags = Dict(
        {
            'user': (
                {'IPClusterNBExtension': {'user': True}},
                'Apply the operation only for the given user',
            )
        }
    )

    def start(self):
        from ipyparallel.nbextension.install import install_extensions

        if len(self.extra_args) != 1:
            self.exit("Must specify 'enable' or 'disable'")
        action = self.extra_args[0].lower()
        if action == 'enable':
            print("Enabling IPython clusters tab")
            install_extensions(enable=True, user=self.user)
        elif action == 'disable':
            print("Disabling IPython clusters tab")
            install_extensions(enable=False, user=self.user)
        else:
            self.exit("Must specify 'enable' or 'disable', not '%s'" % action)


class IPCluster(BaseParallelApplication):
    name = u'ipcluster'
    description = _description
    examples = _main_examples
    version = __version__

    _deprecated_classes = ["IPClusterApp"]

    subcommands = {
        'start': (IPClusterStart, start_help),
        'stop': (IPClusterStop, stop_help),
        'engines': (IPClusterEngines, engines_help),
        'list': (IPClusterList, stop_help),
        'nbextension': (IPClusterNBExtension, IPClusterNBExtension.description),
    }

    # no aliases or flags for parent App
    aliases = Dict()
    flags = Dict()

    def start(self):
        if self.subapp is None:
            keys = ', '.join("'{}'".format(key) for key in self.subcommands.keys())
            print("No subcommand specified. Must specify one of: %s" % keys)
            print()
            self.print_description()
            self.print_subcommands()
            self.exit(1)
        else:
            return self.subapp.start()


main = IPCluster.launch_instance

if __name__ == '__main__':
    main()