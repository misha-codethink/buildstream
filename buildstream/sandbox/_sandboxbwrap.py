#!/usr/bin/env python3
#
#  Copyright (C) 2016 Codethink Limited
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
import os
import sys
import time
import errno
import signal
import subprocess
from contextlib import ExitStack

import psutil

from .. import utils, _signals
from . import Sandbox, SandboxFlags, MountMap


# SandboxBwrap()
#
# Default bubblewrap based sandbox implementation.
#
class SandboxBwrap(Sandbox):

    # Minimal set of devices for the sandbox
    DEVICES = [
        '/dev/full',
        '/dev/null',
        '/dev/urandom',
        '/dev/random',
        '/dev/zero'
    ]

    def run(self, command, flags, cwd=None, env=None):
        stdout, stderr = self._get_output()
        root_directory = self.get_directory()
        scratch_directory = self._get_scratch_directory()
        scratch_tempdir = os.path.join(scratch_directory, 'temp')

        # Fallback to the sandbox default settings for
        # the cwd and env.
        #
        if cwd is None:
            cwd = self._get_work_directory()

        if env is None:
            env = self._get_environment()

        # We want command args as a list of strings
        if type(command) == str:
            command = [command]

        # Create the mount map, this will tell us where
        # each mount point needs to be mounted from and to
        mount_map = MountMap(self, flags & SandboxFlags.ROOT_READ_ONLY)
        root_mount_source = mount_map.get_mount_source('/')

        if cwd is None:
            cwd = '/'

        # Grab the full path of the bwrap binary
        bwrap_command = [utils.get_host_tool('bwrap')]

        # Create a new pid namespace, this also ensures that any subprocesses
        # are cleaned up when the bwrap process exits.
        bwrap_command += ['--unshare-pid']

        # Add in the root filesystem stuff first.
        #
        # The rootfs is mounted as RW initially so that further mounts can be
        # placed on top. If a RO root is required, after all other mounts are
        # complete, root is remounted as RO
        bwrap_command += ["--bind", root_mount_source, "/"]

        if not flags & SandboxFlags.NETWORK_ENABLED:
            bwrap_command += ['--unshare-net']

        if cwd is not None:
            bwrap_command += ['--chdir', cwd]

        # Give it a proc and tmpfs
        bwrap_command += [
            '--proc', '/proc',
            '--tmpfs', '/tmp'
        ]

        # In interactive mode, we want a complete devpts inside
        # the container, so there is a /dev/console and such. In
        # the regular non-interactive sandbox, we want to hand pick
        # a minimal set of devices to expose to the sandbox.
        #
        if flags & SandboxFlags.INTERACTIVE:
            bwrap_command += ['--dev', '/dev']
        else:
            for device in self.DEVICES:
                bwrap_command += ['--dev-bind', device, device]

        # Add bind mounts to any marked directories
        marked_directories = self._get_marked_directories()
        for mark in marked_directories:
            mount_point = mark['directory']
            mount_source = mount_map.get_mount_source(mount_point)
            bwrap_command += ['--bind', mount_source, mount_point]

        if flags & SandboxFlags.ROOT_READ_ONLY:
            bwrap_command += ["--remount-ro", "/"]

        # Set UID and GUI
        bwrap_command += ['--unshare-user', '--uid', '0', '--gid', '0']

        # Add the command
        bwrap_command += command

        # bwrap might create some directories while being suid
        # and may give them to root gid, if it does, we'll want
        # to clean them up after, so record what we already had
        # there just in case so that we can safely cleanup the debris.
        #
        existing_basedirs = {
            directory: os.path.exists(os.path.join(root_directory, directory))
            for directory in ['tmp', 'dev', 'proc']
        }

        # Use the MountMap context manager to ensure that any redirected
        # mounts through fuse layers are in context and ready for bwrap
        # to mount them from.
        #
        with ExitStack() as stack:
            stack.enter_context(mount_map.mounted(self))

            # Ensure the cwd exists
            if cwd is not None:
                workdir = os.path.join(root_mount_source, cwd.lstrip(os.sep))
                os.makedirs(workdir, exist_ok=True)

            # If we're interactive, we want to inherit our stdin,
            # otherwise redirect to /dev/null, ensuring process
            # disconnected from terminal.
            if flags & SandboxFlags.INTERACTIVE:
                stdin = sys.stdin
            else:
                stdin = stack.enter_context(open(os.devnull, "r"))

            # Run bubblewrap !
            exit_code = self.run_bwrap(bwrap_command, stdin, stdout, stderr, env,
                                       (flags & SandboxFlags.INTERACTIVE))

            # Cleanup things which bwrap might have left behind, while
            # everything is still mounted because bwrap can be creating
            # the devices on the fuse mount, so we should remove it there.
            if not flags & SandboxFlags.INTERACTIVE:
                for device in self.DEVICES:
                    device_path = os.path.join(root_mount_source, device.lstrip('/'))

                    # This will remove the device in a loop, allowing some
                    # retries in case the device file leaked by bubblewrap is still busy
                    self.try_remove_device(device_path)

            # Remove /tmp, this is a bwrap owned thing we want to be sure
            # never ends up in an artifact
            for basedir in ['tmp', 'dev', 'proc']:

                # Skip removal of directories which already existed before
                # launching bwrap
                if not existing_basedirs[basedir]:
                    continue

                base_directory = os.path.join(root_mount_source, basedir)
                try:
                    os.rmdir(base_directory)
                except FileNotFoundError:
                    # ignore this, if bwrap cleaned up properly then it's not a problem.
                    #
                    # If the directory was not empty on the other hand, then this is clearly
                    # a bug, bwrap mounted a tempfs here and when it exits, that better be empty.
                    pass

        return exit_code

    def run_bwrap(self, argv, stdin, stdout, stderr, env, interactive):
        # Wrapper around subprocess.Popen() with common settings.
        #
        # This function blocks until the subprocess has terminated.
        #
        # It then returns a tuple of (exit code, stdout output, stderr output).
        # If stdout was not equal to subprocess.PIPE, stdout will be None. Same for
        # stderr.

        # Fetch the process actually launched inside the bwrap sandbox, or the
        # intermediat control bwrap processes.
        #
        # NOTE:
        #   The main bwrap process itself is setuid root and as such we cannot
        #   send it any signals. Since we launch bwrap with --unshare-pid, it's
        #   direct child is another bwrap process which retains ownership of the
        #   pid namespace. This is the right process to kill when terminating.
        #
        #   The grandchild is the binary which we asked bwrap to launch on our
        #   behalf, whatever this binary is, it is the right process to use
        #   for suspending and resuming. In the case that this is a shell, the
        #   shell will be group leader and all build scripts will stop/resume
        #   with that shell.
        #
        def get_user_proc(bwrap_pid, grand_child=False):
            bwrap_proc = psutil.Process(bwrap_pid)
            bwrap_children = bwrap_proc.children()
            if bwrap_children:
                if grand_child:
                    bwrap_grand_children = bwrap_children[0].children()
                    if bwrap_grand_children:
                        return bwrap_grand_children[0]
                else:
                    return bwrap_children[0]
            return None

        def terminate_bwrap():
            if process:
                user_proc = get_user_proc(process.pid)
                if user_proc:
                    user_proc.kill()

        def suspend_bwrap():
            if process:
                user_proc = get_user_proc(process.pid, grand_child=True)
                if user_proc:
                    group_id = os.getpgid(user_proc.pid)
                    os.killpg(group_id, signal.SIGSTOP)

        def resume_bwrap():
            if process:
                user_proc = get_user_proc(process.pid, grand_child=True)
                if user_proc:
                    group_id = os.getpgid(user_proc.pid)
                    os.killpg(group_id, signal.SIGCONT)

        with ExitStack() as stack:

            # We want to launch bwrap in a new session in non-interactive
            # mode so that we handle the SIGTERM and SIGTSTP signals separately
            # from the nested bwrap process, but in interactive mode this
            # causes launched shells to lack job control (we dont really
            # know why that is).
            #
            if interactive:
                new_session = False
            else:
                new_session = True
                stack.enter_context(_signals.suspendable(suspend_bwrap, resume_bwrap))
                stack.enter_context(_signals.terminator(terminate_bwrap))

            process = subprocess.Popen(
                argv,
                # The default is to share file descriptors from the parent process
                # to the subprocess, which is rarely good for sandboxing.
                close_fds=True,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=new_session
            )
            process.communicate()
            exit_code = process.poll()

            if interactive and stdin.isatty():
                # Make this process the foreground process again, otherwise the
                # next read() on stdin will trigger SIGTTIN and stop the process.
                # This is required because the sandboxed process does not have
                # permission to do this on its own (running in separate PID namespace).
                #
                # tcsetpgrp() will trigger SIGTTOU when called from a background
                # process, so ignore it temporarily.
                handler = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                os.tcsetpgrp(0, os.getpid())
                signal.signal(signal.SIGTTOU, handler)

        return exit_code

    def try_remove_device(self, device_path):

        # Put some upper limit on the tries here
        max_tries = 1000
        tries = 0

        while True:
            try:
                os.unlink(device_path)
            except OSError as e:
                if e.errno == errno.EBUSY:
                    # This happens on some machines, seems there is a race sometimes
                    # after bubblewrap returns and the device files it bind-mounted did
                    # not finish unmounting.
                    #
                    if tries < max_tries:
                        tries += 1
                        time.sleep(1 / 100)
                        continue
                    else:
                        # We've reached the upper limit of tries, bail out now
                        # because something must have went wrong
                        #
                        raise
                elif e.errno == errno.ENOENT:
                    # Bubblewrap cleaned it up for us, no problem if we cant remove it
                    break
                else:
                    # Something unexpected, reraise this error
                    raise
            else:
                # Successfully removed the symlink
                break