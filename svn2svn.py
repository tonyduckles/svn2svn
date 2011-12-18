#!/usr/bin/env python
""" 
svn2svn.py

Replicate changesets from one SVN repository to another, 
includes diffs, comments, and Dates of each revision.
It's also possible to retain the Author info if the Target SVN URL
is in a local filesystem (ie, running svn2svn.py on Target SVN server),
or if Target SVN URL is managed through ssh tunnel.
In later case, please run 'ssh-add' (adds RSA or DSA identities to 
the authentication agent) before invoking svn2svn.py.

For example (in Unix environment):
$ exec /usr/bin/ssh-agent $SHELL
$ /usr/bin/ssh-add
Enter passphrase for /home/user/.ssh/id_dsa:
Identity added: /home/user/.ssh/id_dsa (/home/user/.ssh/id_dsa)
$ python ./svn2svn.py -a SOURCE TARGET

Written and used on Ubuntu 7.04 (Feisty Fawn). 
Provided as-is and absolutely no warranty - aka Don't bet your life on it.

This tool re-used some modules from svnclient.py on project hgsvn
(a tool can create Mercurial repository from SVN repository):
http://cheeseshop.python.org/pypi/hgsvn

License: GPLv2, the same as hgsvn.

version 0.1.1; Jul 31, 2007; simford dot dong at gmail dot com
"""

import os
import sys
import time
import locale
import shutil
import select
import calendar
import traceback

from optparse import OptionParser
from subprocess import Popen, PIPE
from datetime import datetime

try:
    from xml.etree import cElementTree as ET
except ImportError:
    try:
        from xml.etree import ElementTree as ET
    except ImportError:
        try:
            import cElementTree as ET
        except ImportError:
            from elementtree import ElementTree as ET

svn_log_args = ['log', '--xml', '-v']
svn_info_args = ['info', '--xml']
svn_checkout_args = ['checkout', '-q']
svn_status_args = ['status', '--xml', '-v', '--ignore-externals']
debug = True
runsvn_verbose = True

# define exception class
class ExternalCommandFailed(RuntimeError):
    """
    An external command failed.
    """

def display_error(message, raise_exception = True):
    """
    Display error message, then terminate.
    """
    print "Error:", message
    print
    if raise_exception:
        raise ExternalCommandFailed
    else:
        sys.exit(1)

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
        return None
else:
    def find_program(name):
        """
        Find the name of the program for Popen.
        On Unix, popen isn't picky about having absolute paths.
        """
        return name

def shell_quote(s):
    if os.name == "nt":
        q = '"'
    else:
        q = "'"
    return q + s.replace('\\', '\\\\').replace("'", "'\"'\"'") + q

locale_encoding = locale.getpreferredencoding()

def run_svn(args, fail_if_stderr=False, encoding="utf-8"):
    """
    Run svn cmd in PIPE
    exit if svn cmd failed
    """
    def _transform_arg(a):
        if isinstance(a, unicode):
            a = a.encode(encoding or locale_encoding)
        elif not isinstance(a, str):
            a = str(a)
        return a
    t_args = map(_transform_arg, args)

    cmd = find_program("svn")
    cmd_string = str(" ".join(map(shell_quote, [cmd] + t_args)))
    if runsvn_verbose:
        print "$", cmd_string
    pipe = Popen([cmd] + t_args, executable=cmd, stdout=PIPE, stderr=PIPE)
    out, err = pipe.communicate()
    if pipe.returncode != 0 or (fail_if_stderr and err.strip()):
        display_error("External program failed (return code %d): %s\n%s"
            % (pipe.returncode, cmd_string, err))
    return out

def svn_date_to_timestamp(svn_date):
    """
    Parse an SVN date as read from the XML output and 
    return the corresponding timestamp.
    """
    # Strip microseconds and timezone (always UTC, hopefully)
    # XXX there are various ISO datetime parsing routines out there,
    # cf. http://seehuhn.de/comp/pdate
    date = svn_date.split('.', 2)[0]
    time_tuple = time.strptime(date, "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(time_tuple)

def parse_svn_info_xml(xml_string):
    """
    Parse the XML output from an "svn info" command and extract 
    useful information as a dict.
    """
    d = {}
    tree = ET.fromstring(xml_string)
    entry = tree.find('.//entry')
    if entry:
        d['url'] = entry.find('url').text
        d['revision'] = int(entry.get('revision'))
        d['repos_url'] = tree.find('.//repository/root').text
        d['last_changed_rev'] = int(tree.find('.//commit').get('revision'))
        d['kind'] = entry.get('kind')
    return d

def parse_svn_log_xml(xml_string):
    """
    Parse the XML output from an "svn log" command and extract 
    useful information as a list of dicts (one per log changeset).
    """
    l = []
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('logentry'):
        d = {}
        d['revision'] = int(entry.get('revision'))
        # Some revisions don't have authors, most notably
        # the first revision in a repository.
        author = entry.find('author')
        d['author'] = author is not None and author.text or None
        d['date'] = svn_date_to_timestamp(entry.find('date').text)
        # Some revisions may have empty commit message
        message = entry.find('msg')
        message = message is not None and message.text is not None \
                        and message.text.strip() or ""
        # Replace DOS return '\r\n' and MacOS return '\r' with unix return '\n'
        d['message'] = message.replace('\r\n', '\n').replace('\n\r', '\n'). \
                               replace('\r', '\n')
        paths = d['changed_paths'] = []
        for path in entry.findall('.//path'):
            copyfrom_rev = path.get('copyfrom-rev')
            if copyfrom_rev:
                copyfrom_rev = int(copyfrom_rev)
            paths.append({
                'path': path.text,
                'action': path.get('action'),
                'copyfrom_path': path.get('copyfrom-path'),
                'copyfrom_revision': copyfrom_rev,
            })
        l.append(d)
    return l

def parse_svn_status_xml(xml_string, base_dir=None):
    """
    Parse the XML output from an "svn status" command and extract 
    useful info as a list of dicts (one per status entry).
    """
    l = []
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('.//entry'):
        d = {}
        path = entry.get('path')
        if base_dir is not None:
            assert path.startswith(base_dir)
            path = path[len(base_dir):].lstrip('/\\')
        d['path'] = path
        wc_status = entry.find('wc-status')
        if wc_status.get('item') == 'external':
            d['type'] = 'external'
        elif wc_status.get('revision') is not None:
            d['type'] = 'normal'
        else:
            d['type'] = 'unversioned'
        l.append(d)
    return l

def get_svn_info(svn_url_or_wc, rev_number=None):
    """
    Get SVN information for the given URL or working copy, 
    with an optionally specified revision number.
    Returns a dict as created by parse_svn_info_xml().
    """
    if rev_number is not None:
        args = [svn_url_or_wc + "@" + str(rev_number)]
    else:
        args = [svn_url_or_wc]
    xml_string = run_svn(svn_info_args + args,
        fail_if_stderr=True)
    return parse_svn_info_xml(xml_string)

def svn_checkout(svn_url, checkout_dir, rev_number=None):
    """
    Checkout the given URL at an optional revision number.
    """
    args = []
    if rev_number is not None:
        args += ['-r', rev_number]
    args += [svn_url, checkout_dir]
    return run_svn(svn_checkout_args + args)

def run_svn_log(svn_url_or_wc, rev_start, rev_end, limit, stop_on_copy=False):
    """
    Fetch up to 'limit' SVN log entries between the given revisions.
    """
    if stop_on_copy:
        args = ['--stop-on-copy']
    else:
        args = []
    if rev_start != 'HEAD' and rev_end != 'HEAD':
        args += ['-r', '%s:%s' % (rev_start, rev_end)]
    args += ['--limit', str(limit), svn_url_or_wc]
    xml_string = run_svn(svn_log_args + args)
    return parse_svn_log_xml(xml_string)

def get_svn_status(svn_wc, flags=None):
    """
    Get SVN status information about the given working copy.
    """
    # Ensure proper stripping by canonicalizing the path
    svn_wc = os.path.abspath(svn_wc)
    args = []
    if flags:
        args += [flags]
    args += [svn_wc]
    xml_string = run_svn(svn_status_args + args)
    return parse_svn_status_xml(xml_string, svn_wc)

def get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=False):
    """
    Get the first SVN log entry in the requested revision range.
    """
    entries = run_svn_log(svn_url, rev_start, rev_end, 1, stop_on_copy)
    if not entries:
        display_error("No SVN log for %s between revisions %s and %s" %
                      (svn_url, rev_start, rev_end))

    return entries[0]

def get_first_svn_log_entry(svn_url, rev_start, rev_end):
    """
    Get the first log entry after/at the given revision number in an SVN branch.
    By default the revision number is set to 0, which will give you the log
    entry corresponding to the branch creaction.

    NOTE: to know whether the branch creation corresponds to an SVN import or
    a copy from another branch, inspect elements of the 'changed_paths' entry
    in the returned dictionary.
    """
    return get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=True)

def get_last_svn_log_entry(svn_url, rev_start, rev_end):
    """
    Get the last log entry before/at the given revision number in an SVN branch.
    By default the revision number is set to HEAD, which will give you the log
    entry corresponding to the latest commit in branch.
    """
    return get_one_svn_log_entry(svn_url, rev_end, rev_start, stop_on_copy=True)


log_duration_threshold = 10.0
log_min_chunk_length = 10

def iter_svn_log_entries(svn_url, first_rev, last_rev):
    """
    Iterate over SVN log entries between first_rev and last_rev.

    This function features chunked log fetching so that it isn't too nasty
    to the SVN server if many entries are requested.
    """
    cur_rev = first_rev
    chunk_length = log_min_chunk_length
    chunk_interval_factor = 1.0
    while last_rev == "HEAD" or cur_rev <= last_rev:
        start_t = time.time()
        stop_rev = min(last_rev, cur_rev + int(chunk_length * chunk_interval_factor))
        entries = run_svn_log(svn_url, cur_rev, stop_rev, chunk_length)
        duration = time.time() - start_t
        if not entries:
            if stop_rev == last_rev:
                break
            cur_rev = stop_rev + 1
            chunk_interval_factor *= 2.0
            continue
        for e in entries:
            yield e
        cur_rev = e['revision'] + 1
        # Adapt chunk length based on measured request duration
        if duration < log_duration_threshold:
            chunk_length = int(chunk_length * 2.0)
        elif duration > log_duration_threshold * 2:
            chunk_length = max(log_min_chunk_length, int(chunk_length / 2.0))

def commit_from_svn_log_entry(entry, files=None, keep_author=False):
    """
    Given an SVN log entry and an optional sequence of files, do an svn commit.
    """
    # This will use the local timezone for displaying commit times
    timestamp = int(entry['date'])
    svn_date = str(datetime.fromtimestamp(timestamp))
    # Uncomment this one one if you prefer UTC commit times
    #svn_date = "%d 0" % timestamp
    if keep_author:
        options = ["ci", "--force-log", "-m", entry['message'] + "\nDate: " + svn_date, "--username", entry['author']]
    else:
        options = ["ci", "--force-log", "-m", entry['message'] + "\nDate: " + svn_date + "\nAuthor: " + entry['author']]
    if files:
        options += list(files)
    run_svn(options)
    print ""

def in_svn(p):
    """
    Check if a given file/folder is under Subversion control.
    Prior to SVN 1.6, we could "cheat" and look for the existence of ".svn" directories.
    With SVN 1.7 and beyond, WC-NG means only a single top-level ".svn" at the root of the working-copy.
    Use "svn status" to check the status of the file/folder.
    TODO: Is there a better way to do this?
    """
    entries = get_svn_status(p)
    if not entries:
      return False
    d = entries[0]
    return (d['type'] == 'normal')

def svn_add_dir(p):
    # set p = "." when p = ""
    #p = p.strip() or "."
    if p.strip() and not in_svn(p):
        # Make sure our parent is under source-control before we try to "svn add" this folder.
        svn_add_dir(os.path.dirname(p))
        if not os.path.exists(p):
            os.makedirs(p)
        run_svn(["add", p])

def find_svn_parent(source_repos_url, source_path, branch_path, branch_rev):
    """
    Given a copy-from path (branch_path), walk the SVN history backwards to inspect
    the ancestory of that path. If we find a "copyfrom_path" which source_path is a
    substring match of, then return that source path. Otherwise, branch_path has no
    ancestory compared to source_path. This is useful when comparing "trunk" vs. "branch"
    paths, to handle cases where a file/folder was renamed in a branch and then that
    branch was merged back to trunk.
    * source_repos_url = Full URL to root of repository, e.g. 'file:///path/to/repos'
    * source_path = e.g. 'trunk/projectA/file1.txt'
    * branch_path = e.g. 'branch/bug123/projectA/file1.txt'
    """

    done = False
    ancestor = { 'path': branch_path, 'revision': branch_rev }
    ancestors = []
    ancestors.append({'path': ancestor['path'], 'revision': ancestor['revision']})
    rev = branch_rev
    path = branch_path
    while not done:
        # Get "svn log" entry for path just before (or at) @rev
        if debug:
            print ">> find_svn_parent: " + source_repos_url + " " + path + "@" + str(rev)
        log_entry = get_last_svn_log_entry(source_repos_url + path + "@" + str(rev), 1, str(rev))
        if not log_entry:
            done = True
        # Update rev so that we go back in time during the next loop
        rev = log_entry['revision']-1
        # Check if our target path was changed in this revision
        for d in log_entry['changed_paths']:
            p = d['path']
            if not p in path:
                continue

            # Check action-type for this file
            action = d['action']
            if action not in 'MARD':
                display_error("In SVN rev. %d: action '%s' not supported. \
                               Please report a bug!" % (svn_rev, action))
            if debug:
                debug_desc = ": " + action + " " + p
                if d['copyfrom_path']:
                    debug_desc += " (from " + d['copyfrom_path'] + "@" + str(d['copyfrom_revision']) + ")"
                print debug_desc

            if action == 'R':
                # If file was replaced, it has no ancestor
                return None
            if action == 'D':
                # If file was deleted, it has no ancestor
                return None
            if action == 'A':
                # If file was added but not a copy, it has no ancestor
                if not d['copyfrom_path']:
                    return None
                p_old = d['copyfrom_path']
                ancestor['path'] = ancestor['path'].replace(p, p_old)
                ancestor['revision'] = d['copyfrom_revision']
                ancestors.append({'path': ancestor['path'], 'revision': ancestor['revision']})
                # If we found a copy-from case which matches our source_path, we're done
                if (p_old == source_path) or (p_old.startswith(source_path + "/")):
                    return ancestors
                # Else, follow the copy and keep on searching
                rev = ancestor['revision']
                path = ancestor['path']
                if debug:
                    print ">> find_svn_parent: copy-from: " + path + "@" + str(rev) + " -- " + ancestor['path']
                break
    return None

def do_svn_copy(source_repos_url, source_path, dest_path, ancestors):
    for ancestor in ancestors:
        break
        # TODO

def pull_svn_rev(log_entry, source_repos_url, source_url, target_url, source_path, original_wc, keep_author=False):
    """
    Pull SVN changes from the given log entry.
    Returns the new SVN revision.
    If an exception occurs, it will rollback to revision 'svn_rev - 1'.
    """
    svn_rev = log_entry['revision']
    run_svn(["up", "--ignore-externals", "-r", svn_rev, original_wc])

    removed_paths = []
    modified_paths = []
    unrelated_paths = []
    commit_paths = []
    for d in log_entry['changed_paths']:
        # Get the full path for this changed_path
        # e.g. u'/branches/xmpp/twisted/words/test/test.py'
        p = d['path']
        if not p.startswith(source_path + "/"):
            # Ignore changed files that are not part of this subdir
            if p != source_path:
                unrelated_paths.append(p)
            continue
        # Calculate the relative path (based on source_path) for this changed_path
        # e.g. u'twisted/words/test/test.py'
        p = p[len(source_path):].strip("/")
        # Record for commit
        action = d['action']

        if debug:
            debug_desc = " " + action + " " + source_path + "/" + p
            if d['copyfrom_path']:
                debug_desc += " (from " + d['copyfrom_path'] + "@" + str(d['copyfrom_revision']) + ")"
            print debug_desc

        if action not in 'MARD':
            display_error("In SVN rev. %d: action '%s' not supported. \
                           Please report a bug!" % (svn_rev, action))

        # Try to be efficient and keep track of an explicit list of paths in the
        # working copy that changed. If we commit from the root of the working copy,
        # then SVN needs to crawl the entire working copy looking for pending changes.
        # But, if we gather too many paths to commit, then we wipe commit_paths below
        # and end-up doing a commit at the root of the working-copy.
        if len (commit_paths) < 100:
            commit_paths.append(p)

        # Special-handling for replace's
        if action == 'R':
            # If file was "replaced" (deleted then re-added, all in same revision),
            # then we need to run the "svn rm" first, then change action='A'. This
            # lets the normal code below handle re-"svn add"'ing the files. This
            # should replicate the "replace".
            run_svn(["up", p])
            run_svn(["remove", "--force", p])
            action = 'A'

        # Handle all the various action-types
        # (Handle "add" first, for "svn copy/move" support)
        if action == 'A':
            # Determine where to export from
            from_rev = svn_rev
            from_path = source_path + "/" + p
            svn_copy = False
            # Handle cases where this "add" was a copy from another URL in the source repos
            if d['copyfrom_revision']:
                from_rev = d['copyfrom_revision']
                from_path = d['copyfrom_path']
                ancestors = find_svn_parent(source_repos_url, source_path, from_path, from_rev)
                if ancestors:
                    parent = ancestors[len(ancestors)-1]
                    #from_rev =  parent['revision']
                    #from_path = parent['path']
                    if debug:
                        print ">> find_svn_parent: FOUND PARENT: " + parent['path'] + "@" + str(parent['revision'])
                        print ancestors
                    # TODO: For copy-from's, need to re-walk the branch history to make sure we handle
                    # any renames correctly.
                    #from_path = from_path[len(source_path):].strip("/")
                    #svn_copy = True

            if svn_copy:
                #do_svn_copy(source_repos_url, source_path
                run_svn(["copy", from_path, p])
            else:
                # Create (parent) directory if needed
                if os.path.isdir(original_wc + os.sep + p):
                    p_path = p
                else:
                    p_path = os.path.dirname(p).strip() or '.'
                if not os.path.exists(p_path):
                    os.makedirs(p_path)

                # Export the entire added tree. Can't use shutil.copytree() since that
                # would copy ".svn" folders on SVN pre-1.7. Also, in cases where the copy-from
                # is from some path in the source_repos _outside_ of our source_path, original_wc
                # won't even have the source files we want to copy.
                run_svn(["export", "--force", "-r", str(from_rev),
                         source_repos_url + from_path + "@" + str(from_rev), p])
                # TODO: Need to copy SVN properties from source repos

                if os.path.isdir(original_wc + os.sep + p):
                    svn_add_dir(p)
                else:
                    p_path = os.path.dirname(p).strip() or '.'
                    svn_add_dir(p_path)
                    run_svn(["add", p])

        elif action == 'D':
            # Queue "svn remove" commands, to allow the action == 'A' handling the opportunity
            # to do smart "svn copy" handling on copy/move/renames.
            removed_paths.append(p)

        elif action == 'R':
            # TODO
            display_error("Internal Error: Handling for action='R' not implemented yet.")

        elif action == 'M':
            modified_paths.append(p)

        else:
            display_error("Internal Error: pull_svn_rev: Unhandled 'action' value: '" + action + "'")

    if removed_paths:
        for r in removed_paths:
            run_svn(["up", r])
            run_svn(["remove", "--force", r])

    if modified_paths:
        for m in modified_paths:
            run_svn(["up", m])
            m_url = source_url + "/" + m
            out = run_svn(["merge", "-c", str(svn_rev), "--non-recursive",
                     "--non-interactive", "--accept=theirs-full",
                     m_url+"@"+str(svn_rev), m])
            # if conflicts, use the copy from original_wc
            if out and out.split()[0] == 'C':
                print "\n### Conflicts ignored: %s, in revision: %s\n" \
                      % (m, svn_rev)
                run_svn(["revert", "--recursive", m])
                if os.path.isfile(m):
                    shutil.copy(original_wc + os.sep + m, m)

    if unrelated_paths:
        print "Unrelated paths: "
        print "*", unrelated_paths

    # If we had too many individual paths to commit, wipe the list and just commit at
    # the root of the working copy.
    if len (commit_paths) > 99:
        commit_paths = []

    try:
        commit_from_svn_log_entry(log_entry, commit_paths, 
                                  keep_author=keep_author)
    except ExternalCommandFailed:
        # try to ignore the Properties conflicts on files and dirs
        # use the copy from original_wc
        has_Conflict = False
        for d in log_entry['changed_paths']:
            p = d['path']
            p = p[len(source_path):].strip("/")
            if os.path.isfile(p):
                if os.path.isfile(p + ".prej"):
                    has_Conflict = True
                    shutil.copy(original_wc + os.sep + p, p)
                    p2=os.sep + p.replace('_', '__').replace('/', '_') \
                              + ".prej-" + str(svn_rev)
                    shutil.move(p + ".prej", os.path.dirname(original_wc) + p2)
                    w="\n### Properties conflicts ignored:"
                    print "%s %s, in revision: %s\n" % (w, p, svn_rev)
            elif os.path.isdir(p):
                if os.path.isfile(p + os.sep + "dir_conflicts.prej"):
                    has_Conflict = True
                    p2=os.sep + p.replace('_', '__').replace('/', '_') \
                              + "_dir__conflicts.prej-" + str(svn_rev)
                    shutil.move(p + os.sep + "dir_conflicts.prej",
                                os.path.dirname(original_wc) + p2)
                    w="\n### Properties conflicts ignored:"
                    print "%s %s, in revision: %s\n" % (w, p, svn_rev)
                    out = run_svn(["propget", "svn:ignore",
                                   original_wc + os.sep + p])
                    if out:
                        run_svn(["propset", "svn:ignore", out.strip(), p])
                    out = run_svn(["propget", "svn:externel",
                                   original_wc + os.sep + p])
                    if out:
                        run_svn(["propset", "svn:external", out.strip(), p])
        # try again
        if has_Conflict:
            commit_from_svn_log_entry(log_entry, commit_paths,
                                      keep_author=keep_author)
        else:
            raise ExternalCommandFailed


def main():
    usage = "Usage: %prog [-a] [-c] [-r SVN rev] <Source SVN URL> <Target SVN URL>"
    parser = OptionParser(usage)
    parser.add_option("-a", "--keep-author", action="store_true",
                      dest="keep_author", help="Keep revision Author or not")
    parser.add_option("-c", "--continue-from-break", action="store_true",
                      dest="cont_from_break",
                      help="Continue from previous break")
    parser.add_option("-r", "--svn-rev", type="int", dest="svn_rev",
                      help="SVN revision to checkout from")
    (options, args) = parser.parse_args()
    if len(args) != 2:
        display_error("incorrect number of arguments\n\nTry: svn2svn.py --help",
                      False)

    source_url = args.pop(0).rstrip("/")
    target_url = args.pop(0).rstrip("/")
    if options.keep_author:
        keep_author = True
    else:
        keep_author = False

    # Find the greatest_rev
    # don't use 'svn info' to get greatest_rev, it doesn't work sometimes
    svn_log = get_one_svn_log_entry(source_url, "HEAD", "HEAD")
    greatest_rev = svn_log['revision']

    original_wc = "_original_wc"
    dup_wc = "_dup_wc"

    ## old working copy does not exist, disable continue mode
    if not os.path.exists(dup_wc):
        options.cont_from_break = False

    if not options.cont_from_break:
        # Warn if Target SVN URL existed
        cmd = find_program("svn")
        pipe = Popen([cmd] + ["list"] + [target_url], executable=cmd,
                     stdout=PIPE, stderr=PIPE)
        out, err = pipe.communicate()
        if pipe.returncode == 0:
            print "Target SVN URL: %s existed!" % target_url
            if out:
                print out
            print "Press 'Enter' to Continue, 'Ctrl + C' to Cancel..."
            print "(Timeout in 5 seconds)"
            rfds, wfds, efds = select.select([sys.stdin], [], [], 5)

        # Get log entry for the SVN revision we will check out
        if options.svn_rev:
            # If specify a rev, get log entry just before or at rev
            svn_start_log = get_last_svn_log_entry(source_url, 1,
                                                   options.svn_rev)
        else:
            # Otherwise, get log entry of branch creation
            svn_start_log = get_first_svn_log_entry(source_url, 1,
                                                    greatest_rev)
    
        # This is the revision we will checkout from
        svn_rev = svn_start_log['revision']
    
        # Check out first revision (changeset) from Source SVN URL
        if os.path.exists(original_wc):
            shutil.rmtree(original_wc)
        svn_checkout(source_url, original_wc, svn_rev)

        # Import first revision (changeset) into Target SVN URL
        timestamp = int(svn_start_log['date'])
        svn_date = str(datetime.fromtimestamp(timestamp))
        if keep_author:
            run_svn(["import", original_wc, target_url, "-m",
                    svn_start_log['message'] + "\nDate: " + svn_date,
                    "--username", svn_start_log['author']])
        else:
            run_svn(["import", original_wc, target_url, "-m",
                    svn_start_log['message'] + "\nDate: " + svn_date +
                    "\nAuthor: " + svn_start_log['author']])
    
        # Check out a working copy
        if os.path.exists(dup_wc):
            shutil.rmtree(dup_wc)
        svn_checkout(target_url, dup_wc)

    original_wc = os.path.abspath(original_wc)
    dup_wc = os.path.abspath(dup_wc)
    os.chdir(dup_wc)

    # Get SVN info
    svn_info = get_svn_info(original_wc)
    # Get the base URL for the source repos
    # e.g. u'svn://svn.twistedmatrix.com/svn/Twisted'
    source_repos_url = svn_info['repos_url']
    # Get the source URL for the source repos
    # e.g. u'svn://svn.twistedmatrix.com/svn/Twisted/branches/xmpp'
    source_url = svn_info['url']
    assert source_url.startswith(source_repos_url)
    # Get the relative offset of source_url based on source_repos_url
    # e.g. u'/branches/xmpp'
    source_path = source_url[len(source_repos_url):]

    if options.cont_from_break:
        svn_rev = svn_info['revision'] - 1
        if svn_rev < 1:
            svn_rev = 1

    # Load SVN log starting from svn_rev + 1
    it_log_entries = iter_svn_log_entries(source_url, svn_rev + 1, greatest_rev)

    try:
        for log_entry in it_log_entries:
            pull_svn_rev(log_entry, source_repos_url, source_url, target_url, source_path,
                         original_wc, keep_author)

    except KeyboardInterrupt:
        print "\nStopped by user."
        run_svn(["cleanup"])
        run_svn(["revert", "--recursive", "."])
    except:
        print "\nCommand failed with following error:\n"
        traceback.print_exc()
        run_svn(["cleanup"])
        run_svn(["revert", "--recursive", "."])
    finally:
        run_svn(["up"])
        print "\nFinished!"


if __name__ == "__main__":
    main()

# vim: shiftwidth=4 softtabstop=4
