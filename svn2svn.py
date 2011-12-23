#!/usr/bin/env python
"""
svn2svn.py

Replicate (replay) changesets from one SVN repository to another:
* Maintains full logical history (e.g. uses "svn copy" for renames).
* Maintains original commit messages.
* Cannot maintain original commit date, but appends original commit date
  for each commit message: "Date: %d".
* Optionally maintain source author info. (Only supported if accessing
  target SVN repo via file://)
* Optionally run an external shell script before each replayed commit
  to give the ability to dynamically exclude or modify files as part
  of the replay.

License: GPLv2, the same as hgsvn.
Author: Tony Duckles (https://github.com/tonyduckles/svn2svn)
(This is a forked and modified verison of http://code.google.com/p/svn2svn/)
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

svn_log_args = ['log', '--xml']
svn_info_args = ['info', '--xml']
svn_checkout_args = ['checkout', '-q']
svn_status_args = ['status', '--xml', '-v', '--ignore-externals']

# Setup debug options
debug = False
debug_runsvn_timing = False    # Display how long each "svn" OS command took to run?
# Setup verbosity options
runsvn_showcmd = False    # Display every "svn" OS command we run?
runsvn_showout = False    # Display the stdout results from every  "svn" OS command we run?
svnlog_verbose = True     # Display each action + changed-path as we walk the history?

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
    if runsvn_showcmd:
        print "$", "("+os.getcwd()+")", cmd_string
    if debug_runsvn_timing:
        time1 = time.time()
    pipe = Popen([cmd] + t_args, executable=cmd, stdout=PIPE, stderr=PIPE)
    out, err = pipe.communicate()
    if debug_runsvn_timing:
        time2 = time.time()
        print "(" + str(round(time2-time1,4)) + " elapsed)"
    if out and runsvn_showout:
        print out
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
                'kind': path.get('kind'),
                'action': path.get('action'),
                'copyfrom_path': path.get('copyfrom-path'),
                'copyfrom_revision': copyfrom_rev,
            })
        # Need to sort paths (i.e. into hierarchical order), so that process_svn_log_entry()
        # can process actions in depth-first order.
        paths.sort()
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
    xml_string = run_svn(svn_info_args + args, fail_if_stderr=True)
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

def run_svn_log(svn_url_or_wc, rev_start, rev_end, limit, stop_on_copy=False, get_changed_paths=True):
    """
    Fetch up to 'limit' SVN log entries between the given revisions.
    """
    if stop_on_copy:
        args = ['--stop-on-copy']
    else:
        args = []
    url = str(svn_url_or_wc)
    if rev_start != 'HEAD' and rev_end != 'HEAD':
        args += ['-r', '%s:%s' % (rev_start, rev_end)]
        if not "@" in svn_url_or_wc:
            url += "@" + str(max(rev_start, rev_end))
    if get_changed_paths:
        args += ['-v']
    args += ['--limit', str(limit), url]
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

def get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=False, get_changed_paths=True):
    """
    Get the first SVN log entry in the requested revision range.
    """
    entries = run_svn_log(svn_url, rev_start, rev_end, 1, stop_on_copy, get_changed_paths)
    if not entries:
        display_error("No SVN log for %s between revisions %s and %s" %
                      (svn_url, rev_start, rev_end))

    return entries[0]

def get_first_svn_log_entry(svn_url, rev_start, rev_end, get_changed_paths=True):
    """
    Get the first log entry after/at the given revision number in an SVN branch.
    By default the revision number is set to 0, which will give you the log
    entry corresponding to the branch creaction.

    NOTE: to know whether the branch creation corresponds to an SVN import or
    a copy from another branch, inspect elements of the 'changed_paths' entry
    in the returned dictionary.
    """
    return get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=True, get_changed_paths=True)

def get_last_svn_log_entry(svn_url, rev_start, rev_end, get_changed_paths=True):
    """
    Get the last log entry before/at the given revision number in an SVN branch.
    By default the revision number is set to HEAD, which will give you the log
    entry corresponding to the latest commit in branch.
    """
    return get_one_svn_log_entry(svn_url, rev_end, rev_start, stop_on_copy=True, get_changed_paths=True)


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
    # TODO: Run optional external shell hook here, for doing pre-commit filtering
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
    print "(Committing source rev #"+str(entry['revision'])+"...)"
    run_svn(options)

def in_svn(p):
    """
    Check if a given file/folder is being tracked by Subversion.
    Prior to SVN 1.6, we could "cheat" and look for the existence of ".svn" directories.
    With SVN 1.7 and beyond, WC-NG means only a single top-level ".svn" at the root of the working-copy.
    Use "svn status" to check the status of the file/folder.
    """
    # TODO: Is there a better way to do this?
    entries = get_svn_status(p)
    if not entries:
      return False
    d = entries[0]
    return (d['type'] == 'normal')

def find_svn_ancestors(source_repos_url, source_base, source_offset, copyfrom_path, copyfrom_rev):
    """
    Given a copy-from path (copyfrom_path), walk the SVN history backwards to inspect
    the ancestory of that path. Build a collection of copyfrom_path+revision pairs
    for each of the branch-copies since the initial branch-creation.  If we find a
    copyfrom_path which source_base is a substring match of (e.g. we crawled back to
    the initial branch-copy from trunk), then return the collection of ancestor paths.
    Otherwise, copyfrom_path has no ancestory compared to source_base.

    This is useful when comparing "trunk" vs. "branch" paths, to handle cases where a
    file/folder was renamed in a branch and then that branch was merged back to trunk.

    PARAMETERS:
    * source_repos_url = Full URL to root of repository, e.g. 'file:///path/to/repos'
    * source_base = e.g. '/trunk'
    * source_offset = e.g. 'projectA/file1.txt'
    * copyfrom_path = e.g. '/branches/bug123/projectA/file1.txt'
    """

    done = False
    working_path = copyfrom_path
    working_base = copyfrom_path[:-len(source_offset)].rstrip('/')
    working_offset = source_offset.strip('/')
    working_rev = copyfrom_rev
    ancestors = [{'path': [working_base, working_offset], 'revision': working_rev}]
    while not done:
        # Get the first "svn log" entry for this path (relative to @rev)
        #working_path = working_base + "/" + working_offset
        if debug:
            print ">> find_svn_ancestors: " + source_repos_url + working_path + "@" + str(working_rev) + \
                   "  (" + working_base + " " + working_offset + ")"
        log_entry = get_first_svn_log_entry(source_repos_url + working_path + "@" + str(working_rev), 1, str(working_rev), True)
        if not log_entry:
            done = True
        # Find the action for our working_path in this revision
        for d in log_entry['changed_paths']:
            path = d['path']
            if not path in working_path:
                continue
            # Check action-type for this file
            action = d['action']
            if action not in 'MARD':
                display_error("In SVN rev. %d: action '%s' not supported. \
                               Please report a bug!" % (log_entry['revision'], action))
            if debug:
                debug_desc = ": " + action + " " + path
                if d['copyfrom_path']:
                    debug_desc += " (from " + d['copyfrom_path'] + "@" + str(d['copyfrom_revision']) + ")"
                print debug_desc

            if action == 'R':
                # If file/folder was replaced, it has no ancestor
                return []
            if action == 'D':
                # If file/folder was deleted, it has no ancestor
                return []
            if action == 'A':
                # If file/folder was added but not a copy, it has no ancestor
                if not d['copyfrom_path']:
                    return []
                # Else, file/folder was added and is a copy, so check ancestors
                path_old = d['copyfrom_path']
                working_path = working_path.replace(path, path_old)
                if working_base in working_path:
                    # If the new and old working_path share the same working_base, just need to update working_offset.
                    working_offset = working_path[len(working_base)+1:]
                else:
                    # Else, assume that working_base has changed but working_offset is the same, e.g. a re-branch.
                    # TODO: Is this a safe assumption?!
                    working_base = working_path[:-len(working_offset)].rstrip('/')
                working_rev = d['copyfrom_revision']
                if debug:
                    print ">> find_svn_ancestors: copy-from: " + working_base + " " + working_offset + "@" + str(working_rev)
                ancestors.append({'path': [working_base, working_offset], 'revision': working_rev})
                # If we found a copy-from case which matches our source_base, we're done
                if (path_old == source_base) or (path_old.startswith(source_base + "/")):
                    return ancestors
                # Else, follow the copy and keep on searching
                break
    return None

def replay_svn_ancestors(ancestors, source_repos_url, source_url, target_url):
    """
    Given an array of ancestor info (find_svn_ancestors), replay the history
    to correctly track renames ("svn copy/move") across branch-merges.

    For example, consider a sequence of events like this:
    1. svn copy /trunk /branches/fix1
    2. (Make some changes on /branches/fix1)
    3. svn copy /branches/fix1/Proj1 /branches/fix1/Proj2  " Rename folder
    4. svn copy /branches/fix1/Proj2/file1.txt /branches/fix1/Proj2/file2.txt  " Rename file inside renamed folder
    5. svn co /trunk && svn merge /branches/fix1
    After the merge and commit, "svn log -v" with show a delete of /trunk/Proj1
    and and add of /trunk/Proj2 comp-from /branches/fix1/Proj2. If we were just
    to do a straight "svn export+add" based on the /branches/fix1/Proj2 folder,
    we'd lose the logical history that Proj2/file2.txt is really a descendant
    of Proj1/file1.txt.

    'source_repos_url' is the full URL to the root of the source repository.
    'ancestors' is the array returned by find_svn_ancestors() with the final
      destination info appended to it by process_svn_log_entry().
    'dest_path'
    """
    # Ignore ancestors[0], which is the original (pre-branch-copy) trunk path
    # Ignore ancestors[1], which is the original branch-creation commit
    # Ignore ancestors[n], which is the final commit back to trunk
    for idx in range(1, len(ancestors)-1):
        ancestor = ancestors[idx]
        source_base = ancestor['path'][0]
        source_offset = ancestor['path'][1]
        source_path = source_base + "/" + source_offset
        source_rev = ancestor['revision']
        source_rev_next = ancestors[idx+1]['revision']
        # Do a "svn log" on the _parent_ directory of source_path, since trying to get log info
        # for the "old path" on the revision where the copy/move happened will fail.
        if "/" in source_path:
            p_source_path = source_path[:source_path.rindex('/')]
        else:
            p_source_path = ""
        if debug:
            print ">> replay_svn_ancestors: ["+str(idx)+"]" + source_path+"@"+str(source_rev) + "  ["+p_source_path+"@"+str(source_rev)+":"+str(source_rev_next-1)+"]"
        it_log_entries = iter_svn_log_entries(source_repos_url+p_source_path, source_rev, source_rev_next-1)
        for log_entry in it_log_entries:
            #print ">> replay_svn_ancestors: log_entry: (" + source_repos_url+source_base + ")"
            #print log_entry
            process_svn_log_entry(log_entry, source_repos_url, source_repos_url+source_base, target_url)

def process_svn_log_entry(log_entry, source_repos_url, source_url, target_url, source_offset=""):
    """
    Process SVN changes from the given log entry.
    Returns array of all the paths in the working-copy that were changed,
    i.e. the paths which need to be "svn commit".

    'log_entry' is the array structure built by parse_svn_log_xml().
    'source_repos_url' is the full URL to the root of the source repository.
    'source_url' is the full URL to the source path in the source repository.
    'target_url' is the full URL to the target path in the target repository.
    """
    # Get the relative offset of source_url based on source_repos_url, e.g. u'/branches/bug123'
    source_base = source_url[len(source_repos_url):]
    if debug:
        print ">> process_svn_log_entry: " + source_url + " (" + source_base + ")"

    svn_rev = log_entry['revision']

    removed_paths = []
    modified_paths = []
    unrelated_paths = []
    commit_paths = []

    for d in log_entry['changed_paths']:
        if svnlog_verbose:
            msg = " " + d['action'] + " " + d['path']
            if d['copyfrom_path']:
                msg += " (from " + d['copyfrom_path'] + "@" + str(d['copyfrom_revision']) + ")"
            print msg
        # Get the full path for this changed_path
        # e.g. u'/branches/bug123/projectA/file1.txt'
        path = d['path']
        if not path.startswith(source_base + "/"):
            # Ignore changed files that are not part of this subdir
            if path != source_base:
                print ">> process_svn_log_entry: Unrelated path: " + path + "  (" + source_base + ")"
                unrelated_paths.append(path)
            continue
        # Calculate the offset (based on source_base) for this changed_path
        # e.g. u'projectA/file1.txt'
        # (path = source_base + "/" + path_offset)
        path_offset = path[len(source_base):].strip("/")
        # Get the action for this path
        action = d['action']
        if action not in 'MARD':
            display_error("In SVN rev. %d: action '%s' not supported. \
                           Please report a bug!" % (svn_rev, action))

        # Try to be efficient and keep track of an explicit list of paths in the
        # working copy that changed. If we commit from the root of the working copy,
        # then SVN needs to crawl the entire working copy looking for pending changes.
        # But, if we gather too many paths to commit, then we wipe commit_paths below
        # and end-up doing a commit at the root of the working-copy.
        if len (commit_paths) < 100:
            commit_paths.append(path_offset)

        # Special-handling for replace's
        is_replace = False
        if action == 'R':
            # If file was "replaced" (deleted then re-added, all in same revision),
            # then we need to run the "svn rm" first, then change action='A'. This
            # lets the normal code below handle re-"svn add"'ing the files. This
            # should replicate the "replace".
            run_svn(["up", path_offset])
            run_svn(["remove", "--force", path_offset])
            action = 'A'
            is_replace = True

        # Handle all the various action-types
        # (Handle "add" first, for "svn copy/move" support)
        if action == 'A':
            # Determine where to export from
            copyfrom_rev = svn_rev
            copyfrom_path = path
            svn_copy = False
            # Handle cases where this "add" was a copy from another URL in the source repos
            if d['copyfrom_revision']:
                copyfrom_rev = d['copyfrom_revision']
                copyfrom_path = d['copyfrom_path']
                if debug:
                    print ">> process_svn_log_entry: copy-to: " + source_base + " " + source_offset + " " + path_offset
                if source_base in copyfrom_path:
                    # If the copy-from path is inside the current working-copy, no need to check ancestry.
                    ancestors = []
                    copyfrom_path = copyfrom_path[len(source_base):].strip("/")
                    if debug:
                        print ">> process_svn_log_entry: Found copy: " + copyfrom_path+"@"+str(copyfrom_rev)
                    svn_copy = True
                else:
                    ancestors = find_svn_ancestors(source_repos_url, source_base, path_offset,
                                                   copyfrom_path, copyfrom_rev)
                if ancestors:
                    # Reverse the list, so that we loop in chronological order
                    ancestors.reverse()
                    # Append the current revision
                    ancestors.append({'path': [source_base, path_offset], 'revision': svn_rev})
                    # ancestors[0] is the original (pre-branch-copy) trunk path.
                    # ancestors[1] is the first commit on the new branch.
                    copyfrom_rev =  ancestors[0]['revision']
                    copyfrom_base = ancestors[0]['path'][0]
                    copyfrom_offset = ancestors[0]['path'][1]
                    copyfrom_path = copyfrom_base + copyfrom_offset
                    if debug:
                        print ">> process_svn_log_entry: FOUND PARENT:"
                        for idx in range(0,len(ancestors)):
                            ancestor = ancestors[idx]
                            print "     ["+str(idx)+"] " + ancestor['path'][0]+" "+ancestor['path'][1]+"@"+str(ancestor['revision'])
                    #print ">> process_svn_log_entry: copyfrom_path (before): " + copyfrom_path + " source_base: " + source_base + " p: " + p
                    copyfrom_path = copyfrom_path[len(source_base):].strip("/")
                    #print ">> process_svn_log_entry: copyfrom_path (after): " + copyfrom_path
                    svn_copy = True
            # If this add was a copy-from, do a smart replay of the ancestors' history.
            # Else just copy/export the files from the source repo and "svn add" them.
            if svn_copy:
                if debug:
                    print ">> process_svn_log_entry: svn_copy: copy-from: " + copyfrom_path+"@"+str(copyfrom_rev) + "  source_base: "+source_base + "  len(ancestors): " + str(len(ancestors))
                # If we don't have any ancestors, then this is just a straight "svn copy" in the current working-copy.
                if not ancestors:
                    # ...but not if the target is already tracked, because this might run several times for the same path.
                    # TODO: Is there a better way to avoid recusion bugs? Maybe a collection of processed paths?
                    if not in_svn(path_offset):
                        run_svn(["copy", copyfrom_path, path_offset])
                else:
                    # Replay any actions which happened to this folder from the ancestor path(s).
                    replay_svn_ancestors(ancestors, source_repos_url, source_url, target_url)
            else:
                # Create (parent) directory if needed
                if d['kind'] == 'dir':
                    p_path = path_offset
                else:
                    p_path = os.path.dirname(path_offset).strip() or '.'
                if not os.path.exists(p_path):
                    os.makedirs(p_path)
                # Export the entire added tree.
                run_svn(["export", "--force", "-r", str(copyfrom_rev),
                         source_repos_url + copyfrom_path + "@" + str(copyfrom_rev), path_offset])
                # TODO: The "no in_svn" condition here is wrong for replace cases.
                #       Added the in_svn condition here originally since "svn export" is recursive
                #       but "svn log" will have an entry for each indiv file, hence we run into a
                #       cannot-re-add-file-which-is-already-added issue.
                if (not in_svn(path_offset)) or (is_replace):
                    run_svn(["add", "--parents", path_offset])
                # TODO: Need to copy SVN properties from source repos

        elif action == 'D':
            # Queue "svn remove" commands, to allow the action == 'A' handling the opportunity
            # to do smart "svn copy" handling on copy/move/renames.
            removed_paths.append(path_offset)

        elif action == 'R':
            # TODO
            display_error("Internal Error: Handling for action='R' not implemented yet.")

        elif action == 'M':
            modified_paths.append(path_offset)

        else:
            display_error("Internal Error: pull_svn_rev: Unhandled 'action' value: '" + action + "'")

    if removed_paths:
        for r in removed_paths:
            # TODO: Is the "svn up" here needed?
            run_svn(["up", r])
            run_svn(["remove", "--force", r])

    if modified_paths:
        for m in modified_paths:
            # TODO: Is the "svn up" here needed?
            run_svn(["up", m])
            m_url = source_url + "/" + m
            out = run_svn(["merge", "-c", str(svn_rev), "--non-recursive",
                     "--non-interactive", "--accept=theirs-full",
                     m_url+"@"+str(svn_rev), m])

    if unrelated_paths:
        print "Unrelated paths: (vs. '" + source_base + "')"
        print "*", unrelated_paths

    return commit_paths

def pull_svn_rev(log_entry, source_repos_url, source_url, target_url, keep_author=False):
    """
    Pull SVN changes from the given log entry.
    Returns the new SVN revision.
    If an exception occurs, it will rollback to revision 'svn_rev - 1'.
    """
    ## Get the relative offset of source_url based on source_repos_url, e.g. u'/branches/bug123'
    #source_base = source_url[len(source_repos_url):]

    svn_rev = log_entry['revision']
    print "\n(Starting source rev #"+str(svn_rev)+":)"
    print "r"+str(log_entry['revision']) + " | " + \
          log_entry['author'] + " | " + \
          str(datetime.fromtimestamp(int(log_entry['date'])).isoformat(' '))
    print log_entry['message']
    print "------------------------------------------------------------------------"
    commit_paths = process_svn_log_entry(log_entry, source_repos_url, source_url, target_url)

    # If we had too many individual paths to commit, wipe the list and just commit at
    # the root of the working copy.
    if len (commit_paths) > 99:
        commit_paths = []

    # TODO: Use SVN properties to track source URL + rev in the target repo?
    #       This would provide a more reliable resume-support
    try:
        commit_from_svn_log_entry(log_entry, commit_paths, keep_author=keep_author)
    except ExternalCommandFailed:
        # try to ignore the Properties conflicts on files and dirs
        # use the copy from original_wc
        # TODO: Need to re-work this?
        #has_Conflict = False
        #for d in log_entry['changed_paths']:
        #    p = d['path']
        #    p = p[len(source_base):].strip("/")
        #    if os.path.isfile(p):
        #        if os.path.isfile(p + ".prej"):
        #            has_Conflict = True
        #            shutil.copy(original_wc + os.sep + p, p)
        #            p2=os.sep + p.replace('_', '__').replace('/', '_') \
        #                      + ".prej-" + str(svn_rev)
        #            shutil.move(p + ".prej", os.path.dirname(original_wc) + p2)
        #            w="\n### Properties conflicts ignored:"
        #            print "%s %s, in revision: %s\n" % (w, p, svn_rev)
        #    elif os.path.isdir(p):
        #        if os.path.isfile(p + os.sep + "dir_conflicts.prej"):
        #            has_Conflict = True
        #            p2=os.sep + p.replace('_', '__').replace('/', '_') \
        #                      + "_dir__conflicts.prej-" + str(svn_rev)
        #            shutil.move(p + os.sep + "dir_conflicts.prej",
        #                        os.path.dirname(original_wc) + p2)
        #            w="\n### Properties conflicts ignored:"
        #            print "%s %s, in revision: %s\n" % (w, p, svn_rev)
        #            out = run_svn(["propget", "svn:ignore",
        #                           original_wc + os.sep + p])
        #            if out:
        #                run_svn(["propset", "svn:ignore", out.strip(), p])
        #            out = run_svn(["propget", "svn:externel",
        #                           original_wc + os.sep + p])
        #            if out:
        #                run_svn(["propset", "svn:external", out.strip(), p])
        ## try again
        #if has_Conflict:
        #    commit_from_svn_log_entry(log_entry, commit_paths, keep_author=keep_author)
        #else:
            raise ExternalCommandFailed
    print "(Finished source rev #"+str(svn_rev)+")"


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

    # Find the greatest_rev in the source repo
    svn_info = get_svn_info(source_url)
    greatest_rev = svn_info['revision']

    dup_wc = "_dup_wc"

    # if old working copy does not exist, disable continue mode
    # TODO: Better continue support. Maybe include source repo's rev # in target commit info?
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
            svn_start_log = get_last_svn_log_entry(source_url, 1, options.svn_rev, False)
        else:
            # Otherwise, get log entry of branch creation
            # TODO: This call is *very* expensive on a repo with lots of revisions.
            #       Even though the call is passing --limit 1, it seems like that limit-filter
            #       is happening after SVN has fetched the full log history.
            svn_start_log = get_first_svn_log_entry(source_url, 1, greatest_rev, False)

        # This is the revision we will start from for source_url
        svn_rev = svn_start_log['revision']

        # Check out a working copy of target_url
        dup_wc = os.path.abspath(dup_wc)
        if os.path.exists(dup_wc):
            shutil.rmtree(dup_wc)
        svn_checkout(target_url, dup_wc)
        os.chdir(dup_wc)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        paths = run_svn(["list", "-r", str(svn_rev), source_url+"@"+str(svn_rev)])
        paths = paths.strip("\n").split("\n")
        for path in paths:
            if not path:
                # Skip null lines
                break
            # Directories have a trailing slash in the "svn list" output
            if path[-1] == "/":
                path=path.rstrip('/')
                if not os.path.exists(path):
                    os.makedirs(path)
            run_svn(["export", "--force", "-r" , str(svn_rev), source_url+"/"+path+"@"+str(svn_rev), path])
            run_svn(["add", path])
        commit_from_svn_log_entry(svn_start_log, [], keep_author)
    else:
        dup_wc = os.path.abspath(dup_wc)
        os.chdir(dup_wc)

    # Get SVN info
    svn_info = get_svn_info(source_url)
    # Get the base URL for the source repos, e.g. u'svn://svn.example.com/svn/repo'
    source_repos_url = svn_info['repos_url']

    if options.cont_from_break:
        svn_rev = svn_info['revision'] - 1
        if svn_rev < 1:
            svn_rev = 1

    # Load SVN log starting from svn_rev + 1
    it_log_entries = iter_svn_log_entries(source_url, svn_rev + 1, greatest_rev)

    try:
        for log_entry in it_log_entries:
            # Replay this revision from source_url into target_url
            pull_svn_rev(log_entry, source_repos_url, source_url, target_url, keep_author)
            # Update our target working-copy, to ensure everything says it's at the new HEAD revision
            run_svn(["up", dup_wc])

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

# vim:sts=4:sw=4:
