""" Shell functions """

from svn2svn import ui
from errors import ExternalCommandFailed

import os
import locale
import time
import shutil
import stat
import sys
import traceback
import re
from datetime import datetime
from subprocess import Popen, PIPE, STDOUT

try:
    import commands
except ImportError:
    commands = None


# Windows compatibility code by Bill Baxter
if os.name == "nt":
    def find_program(name):
        """
        Find the name of the program for Popen.
        Windows is finnicky about having the complete file name. Popen
        won't search the %PATH% for you automatically.
        (Adapted from ctypes.find_library)
        """
        # See MSDN for the REAL search order.
        base, ext = os.path.splitext(name)
        if ext:
            exts = [ext]
        else:
            exts = ['.bat', '.exe']
        for directory in os.environ['PATH'].split(os.pathsep):
            for e in exts:
                fname = os.path.join(directory, base + e)
                if os.path.exists(fname):
                    return fname
        return name
else:
    def find_program(name):
        """
        Find the name of the program for Popen.
        On Unix, popen isn't picky about having absolute paths.
        """
        return name


def _rmtree_error_handler(func, path, exc_info):
    """
    Error handler for rmtree. Helps removing the read-only protection under
    Windows (and others?).
    Adapted from http://www.proaxis.com/~darkwing/hot-backup.py
    and http://patchwork.ozlabs.org/bazaar-ng/patch?id=4243
    """
    if func in (os.remove, os.rmdir) and os.path.exists(path):
        # Change from read-only to writeable
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
        func(path)
    else:
        # something else must be wrong...
        raise

def rmtree(path):
    """
    Wrapper around shutil.rmtree(), to provide more error-resistent behaviour.
    """
    return shutil.rmtree(path, False, _rmtree_error_handler)


# Make sure we do not get localized output from the Suversion
# command line client.
os.environ['LC_MESSAGES'] = 'C'

locale_encoding = locale.getpreferredencoding()

def get_encoding():
    return locale_encoding

def shell_quote(s):
    # No need to wrap "safe" strings in quotes
    if re.compile('^[A-Za-z0-9=-]+$').match(s):
        return s
    if os.name == "nt":
        q = '"'
    else:
        q = "'"
    return q + s.replace('\\', '\\\\').replace("'", "'\"'\"'") + q

def _run_raw_command(cmd, args, fail_if_stderr=False, no_fail=False):
    cmd_string = "%s %s" % (cmd,  " ".join(map(shell_quote, args)))
    color = 'BLUE_B'
    if cmd == 'svn' and args[0] in ['status', 'st', 'log', 'info', 'list', 'proplist', 'propget', 'update', 'up', 'cleanup', 'revert']:
        # Show status-only commands (commands which make no changes to WC) in dim-blue
        color = 'BLUE'
    ui.status("$ %s", cmd_string, level=ui.EXTRA, color=color)
    try:
        pipe = Popen([cmd] + args, executable=cmd, stdout=PIPE, stderr=PIPE)
    except OSError:
        etype, value = sys.exc_info()[:2]
        raise ExternalCommandFailed(
            "Failed running external program: %s\nError: %s"
            % (cmd_string, "".join(traceback.format_exception_only(etype, value))))
    out, err = pipe.communicate()
    if "nothing changed" == out.strip(): # skip this error
        return out
    if (pipe.returncode != 0 or (fail_if_stderr and err.strip())) and not no_fail:
        raise ExternalCommandFailed(
            "External program failed (return code %d): %s\n%s\n%s"
            % (pipe.returncode, cmd_string, err, out))
    return out

def _run_raw_shell_command(cmd, no_fail=False):
    ui.status("* %s", cmd, level=ui.EXTRA, color='BLUE')
    st, out = commands.getstatusoutput(cmd)
    if st != 0 and not no_fail:
        raise ExternalCommandFailed(
            "External program failed with non-zero return code (%d): %s\n%s"
            % (st, cmd, out))
    return out

def run_command(cmd, args=None, bulk_args=None, encoding=None, fail_if_stderr=False, no_fail=False):
    """
    Run a command without using the shell.
    """
    args = args or []
    bulk_args = bulk_args or []
    def _transform_arg(a):
        if isinstance(a, unicode):
            a = a.encode(encoding or locale_encoding or 'UTF-8')
        elif not isinstance(a, str):
            a = str(a)
        return a

    cmd = find_program(cmd)
    if not bulk_args:
        return _run_raw_command(cmd, map(_transform_arg, args), fail_if_stderr, no_fail)
    # If one of bulk_args starts with a dash (e.g. '-foo.php'),
    # svn will take this as an option. Adding '--' ends the search for
    # further options.
    for a in bulk_args:
        if a.strip().startswith('-'):
            args.append("--")
            break
    max_args_num = 254
    i = 0
    out = ""
    while i < len(bulk_args):
        stop = i + max_args_num - len(args)
        sub_args = []
        for a in bulk_args[i:stop]:
            sub_args.append(_transform_arg(a))
        out += _run_raw_command(cmd, args + sub_args, fail_if_stderr, no_fail)
        i = stop
    return out

def run_shell_command(cmd, args=None, bulk_args=None, encoding=None, no_fail=False):
    """
    Run a shell command, properly quoting and encoding arguments.
    Probably only works on Un*x-like systems.
    """
    def _quote_arg(a):
        if isinstance(a, unicode):
            a = a.encode(encoding or locale_encoding)
        elif not isinstance(a, str):
            a = str(a)
        return shell_quote(a)

    if args:
        cmd += " " + " ".join(_quote_arg(a) for a in args)
    max_args_num = 254
    i = 0
    out = ""
    if not bulk_args:
        return _run_raw_shell_command(cmd, no_fail)
    while i < len(bulk_args):
        stop = i + max_args_num - len(args)
        sub_args = []
        for a in bulk_args[i:stop]:
            sub_args.append(_quote_arg(a))
        sub_cmd = cmd + " " + " ".join(sub_args)
        out += _run_raw_shell_command(sub_cmd, no_fail)
        i = stop
    return out

def run_svn(args=None, bulk_args=None, fail_if_stderr=False,
            mask_atsign=False, no_fail=False):
    """
    Run an SVN command, returns the (bytes) output.
    """
    if mask_atsign:
        # The @ sign in Subversion revers to a pegged revision number.
        # SVN treats files with @ in the filename a bit special.
        # See: http://stackoverflow.com/questions/1985203
        for idx in range(len(args)):
            if "@" in args[idx] and args[idx][0] not in ("-", '"'):
                args[idx] = "%s@" % args[idx]
        if bulk_args:
            for idx in range(len(bulk_args)):
                if ("@" in bulk_args[idx]
                    and bulk_args[idx][0] not in ("-", '"')):
                    bulk_args[idx] = "%s@" % bulk_args[idx]
    return run_command("svn",
        args=args, bulk_args=bulk_args, fail_if_stderr=fail_if_stderr, no_fail=no_fail)

def skip_dirs(paths, basedir="."):
    """
    Skip all directories from path list, including symbolic links to real dirs.
    """
    # NOTE: both tests are necessary (Cameron Hutchison's patch for symbolic
    # links to directories)
    return [p for p in paths
        if not os.path.isdir(os.path.join(basedir, p))
        or os.path.islink(os.path.join(basedir, p))]

def get_script_name():
    """Helper to return the name of the command line script that was called."""
    return os.path.basename(sys.argv[0])
