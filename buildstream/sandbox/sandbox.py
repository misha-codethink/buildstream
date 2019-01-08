#
#  Copyright (C) 2017 Codethink Limited
#  Copyright (C) 2018 Bloomberg Finance LP
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Andrew Leeming <andrew.leeming@codethink.co.uk>
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>
"""
Sandbox - The build sandbox
===========================
:class:`.Element` plugins which want to interface with the sandbox
need only understand this interface, while it may be given a different
sandbox implementation, any sandbox implementation it is given will
conform to this interface.

See also: :ref:`sandboxing`.
"""

import os
import shlex
import contextlib
from contextlib import contextmanager

from .._exceptions import ImplError, BstError, SandboxError
from .._message import Message, MessageType
from ..storage._filebaseddirectory import FileBasedDirectory
from ..storage._casbaseddirectory import CasBasedDirectory


class SandboxFlags():
    """Flags indicating how the sandbox should be run.
    """

    NONE = 0
    """Use default sandbox configuration.
    """

    ROOT_READ_ONLY = 0x01
    """The root filesystem is read only.

    This is normally true except when running integration commands
    on staged dependencies, where we have to update caches and run
    things such as ldconfig.
    """

    NETWORK_ENABLED = 0x02
    """Whether to expose host network.

    This should not be set when running builds, but can
    be allowed for running a shell in a sandbox.
    """

    INTERACTIVE = 0x04
    """Whether to run the sandbox interactively

    This determines if the sandbox should attempt to connect
    the terminal through to the calling process, or detach
    the terminal entirely.
    """

    INHERIT_UID = 0x08
    """Whether to use the user id and group id from the host environment

    This determines if processes in the sandbox should run with the
    same user id and group id as BuildStream itself. By default,
    processes run with user id and group id 0, protected by a user
    namespace where available.
    """


class SandboxCommandError(SandboxError):
    """Raised by :class:`.Sandbox` implementations when a command fails.

    Args:
       message (str): The error message to report to the user
       collect (str): An optional directory containing partial install contents
    """
    def __init__(self, message, *, collect=None):
        super().__init__(message, reason='command-failed')

        self.collect = collect


class Sandbox():
    """Sandbox()

    Sandbox programming interface for :class:`.Element` plugins.
    """

    # Minimal set of devices for the sandbox
    DEVICES = [
        '/dev/urandom',
        '/dev/random',
        '/dev/zero',
        '/dev/null'
    ]

    def __init__(self, context, project, directory, **kwargs):
        self.__context = context
        self.__project = project
        self.__directories = []
        self.__cwd = None
        self.__env = None
        self.__mount_sources = {}
        self.__allow_real_directory = kwargs['allow_real_directory']

        # Plugin ID for logging
        plugin = kwargs.get('plugin', None)
        if plugin:
            self.__plugin_id = plugin._get_unique_id()
        else:
            self.__plugin_id = None

        # Configuration from kwargs common to all subclasses
        self.__config = kwargs['config']
        self.__stdout = kwargs['stdout']
        self.__stderr = kwargs['stderr']
        self.__bare_directory = kwargs['bare_directory']

        # Setup the directories. Root and output_directory should be
        # available to subclasses, hence being single-underscore. The
        # others are private to this class.
        # If the directory is bare, it probably doesn't need scratch
        if self.__bare_directory:
            self._root = directory
            self.__scratch = None
            os.makedirs(self._root, exist_ok=True)
        else:
            self._root = os.path.join(directory, 'root')
            self.__scratch = os.path.join(directory, 'scratch')
            for directory_ in [self._root, self.__scratch]:
                os.makedirs(directory_, exist_ok=True)

        self._output_directory = None
        self._vdir = None
        self._usebuildtree = False

        # This is set if anyone requests access to the underlying
        # directory via get_directory.
        self._never_cache_vdirs = False

        # Pending command batch
        self.__batch = None

    def get_directory(self):
        """Fetches the sandbox root directory

        The root directory is where artifacts for the base
        runtime environment should be staged. Only works if
        BST_VIRTUAL_DIRECTORY is not set.

        Returns:
           (str): The sandbox root directory

        """
        if self.__allow_real_directory:
            self._never_cache_vdirs = True
            return self._root
        else:
            raise BstError("You can't use get_directory")

    def get_virtual_directory(self):
        """Fetches the sandbox root directory as a virtual Directory.

        The root directory is where artifacts for the base
        runtime environment should be staged.

        Use caution if you use get_directory and
        get_virtual_directory.  If you alter the contents of the
        directory returned by get_directory, all objects returned by
        get_virtual_directory or derived from them are invalid and you
        must call get_virtual_directory again to get a new copy.

        Returns:
           (Directory): The sandbox root directory

        """
        if self._vdir is None or self._never_cache_vdirs:
            if 'BST_CAS_DIRECTORIES' in os.environ:
                self._vdir = CasBasedDirectory(self.__context.artifactcache.cas, ref=None)
            else:
                self._vdir = FileBasedDirectory(self._root)
        return self._vdir

    def _set_virtual_directory(self, virtual_directory):
        """ Sets virtual directory. Useful after remote execution
        has rewritten the working directory.
        """
        self._vdir = virtual_directory

    def set_environment(self, environment):
        """Sets the environment variables for the sandbox

        Args:
           environment (dict): The environment variables to use in the sandbox
        """
        self.__env = environment

    def set_work_directory(self, directory):
        """Sets the work directory for commands run in the sandbox

        Args:
           directory (str): An absolute path within the sandbox
        """
        self.__cwd = directory

    def set_output_directory(self, directory):
        """Sets the output directory - the directory which is preserved
        as an artifact after assembly.

        Args:
           directory (str): An absolute path within the sandbox
        """
        self._output_directory = directory

    def mark_directory(self, directory, *, artifact=False):
        """Marks a sandbox directory and ensures it will exist

        Args:
           directory (str): An absolute path within the sandbox to mark
           artifact (bool): Whether the content staged at this location
                            contains artifacts

        .. note::
           Any marked directories will be read-write in the sandboxed
           environment, only the root directory is allowed to be readonly.
        """
        self.__directories.append({
            'directory': directory,
            'artifact': artifact
        })

    def run(self, command, flags, *, cwd=None, env=None, label=None):
        """Run a command in the sandbox.

        If this is called outside a batch context, the command is immediately
        executed.

        If this is called in a batch context, the command is added to the batch
        for later execution. If the command fails, later commands will not be
        executed. Command flags must match batch flags.

        Args:
            command (list): The command to run in the sandboxed environment, as a list
                            of strings starting with the binary to run.
            flags (:class:`.SandboxFlags`): The flags for running this command.
            cwd (str): The sandbox relative working directory in which to run the command.
            env (dict): A dictionary of string key, value pairs to set as environment
                        variables inside the sandbox environment.
            label (str): An optional label for the command, used for logging. (*Since: 1.4*)

        Returns:
            (int|None): The program exit code, or None if running in batch context.

        Raises:
            (:class:`.ProgramNotFoundError`): If a host tool which the given sandbox
                                              implementation requires is not found.

        .. note::

           The optional *cwd* argument will default to the value set with
           :func:`~buildstream.sandbox.Sandbox.set_work_directory` and this
           function must make sure the directory will be created if it does
           not exist yet, even if a workspace is being used.
        """

        # Fallback to the sandbox default settings for
        # the cwd and env.
        #
        cwd = self._get_work_directory(cwd=cwd)
        env = self._get_environment(cwd=cwd, env=env)

        # Convert single-string argument to a list
        if isinstance(command, str):
            command = [command]

        if self.__batch:
            assert flags == self.__batch.flags, \
                "Inconsistent sandbox flags in single command batch"

            batch_command = _SandboxBatchCommand(command, cwd=cwd, env=env, label=label)

            current_group = self.__batch.current_group
            current_group.append(batch_command)
            return None
        else:
            return self._run(command, flags, cwd=cwd, env=env)

    @contextmanager
    def batch(self, flags, *, label=None, collect=None):
        """Context manager for command batching

        This provides a batch context that defers execution of commands until
        the end of the context. If a command fails, the batch will be aborted
        and subsequent commands will not be executed.

        Command batches may be nested. Execution will start only when the top
        level batch context ends.

        Args:
            flags (:class:`.SandboxFlags`): The flags for this command batch.
            label (str): An optional label for the batch group, used for logging.
            collect (str): An optional directory containing partial install contents
                           on command failure.

        Raises:
            (:class:`.SandboxCommandError`): If a command fails.

        *Since: 1.4*
        """

        group = _SandboxBatchGroup(label=label)

        if self.__batch:
            # Nested batch
            assert flags == self.__batch.flags, \
                "Inconsistent sandbox flags in single command batch"

            parent_group = self.__batch.current_group
            parent_group.append(group)
            self.__batch.current_group = group
            try:
                yield
            finally:
                self.__batch.current_group = parent_group
        else:
            # Top-level batch
            batch = self._create_batch(group, flags, collect=collect)

            self.__batch = batch
            try:
                yield
            finally:
                self.__batch = None

            batch.execute()

    #####################################################
    #    Abstract Methods for Sandbox implementations   #
    #####################################################

    # _run()
    #
    # Abstract method for running a single command
    #
    # Args:
    #    command (list): The command to run in the sandboxed environment, as a list
    #                    of strings starting with the binary to run.
    #    flags (:class:`.SandboxFlags`): The flags for running this command.
    #    cwd (str): The sandbox relative working directory in which to run the command.
    #    env (dict): A dictionary of string key, value pairs to set as environment
    #                variables inside the sandbox environment.
    #
    # Returns:
    #    (int): The program exit code.
    #
    def _run(self, command, flags, *, cwd, env):
        raise ImplError("Sandbox of type '{}' does not implement _run()"
                        .format(type(self).__name__))

    # _create_batch()
    #
    # Abstract method for creating a batch object. Subclasses can override
    # this method to instantiate a subclass of _SandboxBatch.
    #
    # Args:
    #    main_group (:class:`_SandboxBatchGroup`): The top level batch group.
    #    flags (:class:`.SandboxFlags`): The flags for commands in this batch.
    #    collect (str): An optional directory containing partial install contents
    #                   on command failure.
    #
    def _create_batch(self, main_group, flags, *, collect=None):
        return _SandboxBatch(self, main_group, flags, collect=collect)

    ################################################
    #               Private methods                #
    ################################################
    # _get_context()
    #
    # Fetches the context BuildStream was launched with.
    #
    # Returns:
    #    (Context): The context of this BuildStream invocation
    def _get_context(self):
        return self.__context

    # _get_project()
    #
    # Fetches the Project this sandbox was created to build for.
    #
    # Returns:
    #    (Project): The project this sandbox was created for.
    def _get_project(self):
        return self.__project

    # _get_marked_directories()
    #
    # Fetches the marked directories in the sandbox
    #
    # Returns:
    #    (list): A list of directory mark objects.
    #
    # The returned objects are dictionaries with the following attributes:
    #    directory: The absolute path within the sandbox
    #    artifact: Whether the path will contain artifacts or not
    #
    def _get_marked_directories(self):
        return self.__directories

    # _get_mount_source()
    #
    # Fetches the list of mount sources
    #
    # Returns:
    #    (dict): A dictionary where keys are mount points and values are the mount sources
    def _get_mount_sources(self):
        return self.__mount_sources

    # _set_mount_source()
    #
    # Sets the mount source for a given mountpoint
    #
    # Args:
    #    mountpoint (str): The absolute mountpoint path inside the sandbox
    #    mount_source (str): the host path to be mounted at the mount point
    def _set_mount_source(self, mountpoint, mount_source):
        self.__mount_sources[mountpoint] = mount_source

    # _get_environment()
    #
    # Fetches the environment variables for running commands
    # in the sandbox.
    #
    # Args:
    #    cwd (str): The working directory the command has been requested to run in, if any.
    #    env (str): The environment the command has been requested to run in, if any.
    #
    # Returns:
    #    (str): The sandbox work directory
    def _get_environment(self, *, cwd=None, env=None):
        cwd = self._get_work_directory(cwd=cwd)
        if env is None:
            env = self.__env

        # Naive getcwd implementations can break when bind-mounts to different
        # paths on the same filesystem are present. Letting the command know
        # what directory it is in makes it unnecessary to call the faulty
        # getcwd.
        env = dict(env)
        env['PWD'] = cwd

        return env

    # _get_work_directory()
    #
    # Fetches the working directory for running commands
    # in the sandbox.
    #
    # Args:
    #    cwd (str): The working directory the command has been requested to run in, if any.
    #
    # Returns:
    #    (str): The sandbox work directory
    def _get_work_directory(self, *, cwd=None):
        return cwd or self.__cwd or '/'

    # _get_scratch_directory()
    #
    # Fetches the sandbox scratch directory, this directory can
    # be used by the sandbox implementation to cache things or
    # redirect temporary fuse mounts.
    #
    # The scratch directory is guaranteed to be on the same
    # filesystem as the root directory.
    #
    # Returns:
    #    (str): The sandbox scratch directory
    def _get_scratch_directory(self):
        assert not self.__bare_directory, "Scratch is not going to work with bare directories"
        return self.__scratch

    # _get_output()
    #
    # Fetches the stdout & stderr
    #
    # Returns:
    #    (file): The stdout, or None to inherit
    #    (file): The stderr, or None to inherit
    def _get_output(self):
        return (self.__stdout, self.__stderr)

    # _get_config()
    #
    # Fetches the sandbox configuration object.
    #
    # Returns:
    #    (SandboxConfig): An object containing the configuration
    #              data passed in during construction.
    def _get_config(self):
        return self.__config

    # _has_command()
    #
    #  Tests whether a command exists inside the sandbox
    #
    #     Args:
    #         command (list): The command to test.
    #         env (dict): A dictionary of string key, value pairs to set as environment
    #                     variables inside the sandbox environment.
    #     Returns:
    #         (bool): Whether a command exists inside the sandbox.
    def _has_command(self, command, env=None):
        if os.path.isabs(command):
            return os.path.exists(os.path.join(
                self._root, command.lstrip(os.sep)))

        for path in env.get('PATH').split(':'):
            if os.path.exists(os.path.join(
                    self._root, path.lstrip(os.sep), command)):
                return True

        return False

    # _get_plugin_id()
    #
    # Get the plugin's unique identifier
    #
    def _get_plugin_id(self):
        return self.__plugin_id

    # _callback()
    #
    # If this is called outside a batch context, the specified function is
    # invoked immediately.
    #
    # If this is called in a batch context, the function is added to the batch
    # for later invocation.
    #
    # Args:
    #    callback (callable): The function to invoke
    #
    def _callback(self, callback):
        if self.__batch:
            batch_call = _SandboxBatchCall(callback)

            current_group = self.__batch.current_group
            current_group.append(batch_call)
        else:
            callback()


# _SandboxBatch()
#
# A batch of sandbox commands.
#
class _SandboxBatch():

    def __init__(self, sandbox, main_group, flags, *, collect=None):
        self.sandbox = sandbox
        self.main_group = main_group
        self.current_group = main_group
        self.flags = flags
        self.collect = collect

    def execute(self):
        self.main_group.execute(self)

    def execute_group(self, group):
        if group.label:
            context = self.sandbox._get_context()
            cm = context.timed_activity(group.label, unique_id=self.sandbox._get_plugin_id())
        else:
            cm = contextlib.suppress()

        with cm:
            group.execute_children(self)

    def execute_command(self, command):
        if command.label:
            context = self.sandbox._get_context()
            message = Message(self.sandbox._get_plugin_id(), MessageType.STATUS,
                              'Running command', detail=command.label)
            context.message(message)

        exitcode = self.sandbox._run(command.command, self.flags, cwd=command.cwd, env=command.env)
        if exitcode != 0:
            cmdline = ' '.join(shlex.quote(cmd) for cmd in command.command)
            label = command.label or cmdline
            raise SandboxCommandError("Command '{}' failed with exitcode {}".format(label, exitcode),
                                      collect=self.collect)

    def execute_call(self, call):
        call.callback()


# _SandboxBatchItem()
#
# An item in a command batch.
#
class _SandboxBatchItem():

    def __init__(self, *, label=None):
        self.label = label


# _SandboxBatchCommand()
#
# A command item in a command batch.
#
class _SandboxBatchCommand(_SandboxBatchItem):

    def __init__(self, command, *, cwd, env, label=None):
        super().__init__(label=label)

        self.command = command
        self.cwd = cwd
        self.env = env

    def execute(self, batch):
        batch.execute_command(self)


# _SandboxBatchGroup()
#
# A group in a command batch.
#
class _SandboxBatchGroup(_SandboxBatchItem):

    def __init__(self, *, label=None):
        super().__init__(label=label)

        self.children = []

    def append(self, item):
        self.children.append(item)

    def execute(self, batch):
        batch.execute_group(self)

    def execute_children(self, batch):
        for item in self.children:
            item.execute(batch)


# _SandboxBatchCall()
#
# A call item in a command batch.
#
class _SandboxBatchCall(_SandboxBatchItem):

    def __init__(self, callback):
        super().__init__()

        self.callback = callback

    def execute(self, batch):
        batch.execute_call(self)
