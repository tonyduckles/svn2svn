#!/usr/bin/env python
"""
svn2svn.py

Replicate (replay) changesets from one SVN repository to another:
* Maintains full logical history (e.g. uses "svn copy" for renames).
* Maintains original commit messages.
* Optionally maintain source author info. (Only supported if accessing
  target SVN repo via file://)
* Cannot maintain original commit date, but appends original commit date
  for each commit message: "Date: %d".
* Optionally run an external shell script before each replayed commit
  to give the ability to dynamically exclude or modify files as part
  of the replay.

License: GPLv2, the same as hgsvn.
Author: Tony Duckles (https://github.com/tonyduckles/svn2svn)
(This is a forked and heavily modified verison of http://code.google.com/p/svn2svn/)
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
from operator import itemgetter

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
runsvn_timing = False     # Display how long each "svn" OS command took to run?
# Setup verbosity options
runsvn_showcmd = False    # Display every "svn" OS command we run?
runsvn_showout = False    # Display the stdout results from every  "svn" OS command we run?
svnlog_verbose = False    # Display each action + changed-path as we walk the history?

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
    if runsvn_showcmd:
        import re
        p = re.compile('^[A-Za-z0-9=-]+$')
        if p.match(s):
            return s
    if os.name == "nt":
        q = '"'
    else:
        q = "'"
    return q + s.replace('\\', '\\\\').replace("'", "'\"'\"'") + q

locale_encoding = locale.getpreferredencoding()

def run_svn(args, fail_if_stderr=False, ignore_retcode_err=False, encoding="utf-8"):
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
        # Default to bright-blue for svn commands that will take action on the working-copy.
        color = "94"
        # For status-only commands (or commands that aren't important to highlight), show in dim-blue.
        status_cmds = ['status', 'st', 'log', 'info', 'list', 'propset', 'update', 'up', 'cleanup', 'revert']
        if args[0] in status_cmds:
            color = "34"
        print "\x1b[34m"+"$"+"\x1b["+color+"m", cmd_string + "\x1b[0m"
    if runsvn_timing:
        time1 = time.time()
    pipe = Popen([cmd] + t_args, executable=cmd, stdout=PIPE, stderr=PIPE)
    out, err = pipe.communicate()
    if runsvn_timing:
        time2 = time.time()
        print "(" + str(round(time2-time1,4)) + " elapsed)"
    if out and runsvn_showout:
        print out
    if (pipe.returncode != 0 and not ignore_retcode_err) or (fail_if_stderr and err.strip()):
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
        d['repos_uuid'] = tree.find('.//repository/uuid').text
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
        revprops = []
        for prop in entry.findall('.//revprops/property'):
            revprops.append({ 'name': prop.get('name'), 'value': prop.text })
        d['revprops'] = revprops
        paths = []
        for path in entry.findall('.//paths/path'):
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
        d['changed_paths'] = sorted(paths, key=itemgetter('path'))
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
        elif wc_status.get('item') == 'deleted':
            d['type'] = 'deleted'
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

def run_svn_log(svn_url_or_wc, rev_start, rev_end, limit, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
    """
    Fetch up to 'limit' SVN log entries between the given revisions.
    """
    args = []
    if stop_on_copy:
        args += ['--stop-on-copy']
    if get_changed_paths:
        args += ['-v']
    if get_revprops:
        args += ['--with-all-revprops']
    url = str(svn_url_or_wc)
    if rev_start != 'HEAD' and rev_end != 'HEAD':
        args += ['-r', '%s:%s' % (rev_start, rev_end)]
        if not "@" in svn_url_or_wc:
            url += "@" + str(max(rev_start, rev_end))
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

def get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
    """
    Get the first SVN log entry in the requested revision range.
    """
    entries = run_svn_log(svn_url, rev_start, rev_end, 1, stop_on_copy, get_changed_paths, get_revprops)
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

def iter_svn_log_entries(svn_url, first_rev, last_rev, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
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
        entries = run_svn_log(svn_url, cur_rev, stop_rev, chunk_length, stop_on_copy , get_changed_paths, get_revprops)
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

def commit_from_svn_log_entry(entry, files=None, keep_author=False, revprops=[]):
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
    if revprops:
        for r in revprops:
            options += ["--with-revprop", r['name']+"="+str(r['value'])]
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

def find_svn_ancestors(source_repos_url, source_url, path_offset, source_rev, \
                       copyfrom_path, copyfrom_rev, prefix = ""):
    """
    Given a final svn-add'd path (source_base+"/"+path_offset) and the origin copy-from
    path (copyfrom_path), walk the SVN history backwards to inspect the ancestory of
    that path. Build a collection of copyfrom_path+revision pairs for each of the
    branch-copies since the initial branch-creation.  If we find a copyfrom_path which
    source_url is a substring match of (e.g. we crawled back to the initial branch-
    copy from trunk), then return the collection of ancestor paths.  Otherwise,
    copyfrom_path has no ancestory compared to source_url.

    This is useful when comparing "trunk" vs. "branch" paths, to handle cases where a
    file/folder was renamed in a branch and then that branch was merged back to trunk.

    'source_repos_url' is the full URL to the root of the source repository,
      e.g. 'file:///path/to/repo'
    'source_url' is the full URL to the source path in the source repository.
    'path_offset' is the offset from source_base to the file to check ancestry for,
      e.g. 'projectA/file1.txt'. path = source_repos_url + source_base + path_offset.
    'source_rev' is the revision ("svn log") that we're processing from the source repo.
    'copyfrom_path' is copy-from path, e.g. '/branches/bug123/projectA/file1.txt'
    'copyfrom_rev' is revision this copy-from path was copied at.
    """
    done = False
    source_base = source_url[len(source_repos_url):]
    working_path = copyfrom_path
    working_rev =  copyfrom_rev
    ancestors_temp = [{'path': source_base+"/"+path_offset, 'revision': source_rev, 'copyfrom_path': copyfrom_path, 'copyfrom_rev': copyfrom_rev}]
    while not done:
        # Get the first "svn log" entry for this path (relative to @rev)
        #working_path = working_base + "/" + working_offset
        if debug:
            print prefix+"\x1b[33m" + ">> find_svn_ancestors: " + source_repos_url + working_path+"@"+str(working_rev) + "\x1b[0m"
        log_entry = get_first_svn_log_entry(source_repos_url + working_path+"@"+str(working_rev), 1, str(working_rev), True)
        if not log_entry:
            done = True
            break
        # Search for any actions on our target path (or parent paths).
        changed_paths_temp = []
        for d in log_entry['changed_paths']:
            path = d['path']
            if path in working_path:
                changed_paths_temp.append({'path': path, 'data': d})
        if not changed_paths_temp:
            # If no matches, then we've hit the end of the chain and this path has no ancestry back to source_url.
            done = True
            continue
        # Reverse-sort any matches, so that we start with the most-granular (deepest in the tree) path.
        changed_paths = sorted(changed_paths_temp, key=itemgetter('path'), reverse=True)
        # Find the action for our working_path in this revision
        for v in changed_paths:
            d = v['data']
            path = d['path']
            # Check action-type for this file
            action = d['action']
            if action not in 'MARD':
                display_error("In SVN rev. %d: action '%s' not supported. \
                               Please report a bug!" % (log_entry['revision'], action))
            if debug:
                debug_desc = "> " + action + " " + path
                if d['copyfrom_path']:
                    debug_desc += " (from " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + ")"
                print prefix+"\x1b[33m" + debug_desc + "\x1b[0m"

            if action == 'R':
                # If file/folder was replaced, it has no ancestor
                ancestors_temp = []
                done = True
                break
            if action == 'D':
                # If file/folder was deleted, it has no ancestor
                ancestors_temp = []
                done = True
                break
            if action == 'A':
                # If file/folder was added but not a copy, it has no ancestor
                if not d['copyfrom_path']:
                    ancestors_temp = []
                    done = True
                    break
                # Else, file/folder was added and is a copy, so add an entry to our ancestors list
                # and keep checking for ancestors
                if debug:
                    print prefix+"\x1b[33m" + ">> find_svn_ancestors: Found copy-from: " + \
                          path + " --> " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + "\x1b[0m"
                ancestors_temp.append({'path': path, 'revision': log_entry['revision'],
                                       'copyfrom_path': d['copyfrom_path'], 'copyfrom_rev': d['copyfrom_revision']})
                working_path = working_path.replace(d['path'], d['copyfrom_path'])
                working_rev =  d['copyfrom_revision']
                # If we found a copy-from case which matches our source_base, we're done
                if source_base in working_path:
                    done = True
                    break
                # Else, follow the copy and keep on searching
                break
    ancestors = []
    if ancestors_temp:
        working_path = source_base+"/"+path_offset
        for idx in range(0, len(ancestors_temp)):
            d = ancestors_temp[idx]
            working_path = working_path.replace(d['path'], d['copyfrom_path'])
            working_rev =  d['copyfrom_rev']
            ancestors.append({'path': working_path, 'revision': working_rev})
        if debug:
            max_len = 0
            for idx in range(len(ancestors)):
                d = ancestors[idx]
                max_len = max(max_len, len(d['path']+"@"+str(d['revision'])))
            print prefix+"\x1b[93m" + ">> find_svn_ancestors: Found parent ancestors: " + "\x1b[0m"
            for idx in range(len(ancestors)-1):
                d = ancestors[idx]
                d_next = ancestors[idx+1]
                print prefix+"\x1b[33m" + "  ["+str(idx)+"] " + str(d['path']+"@"+str(d['revision'])).ljust(max_len) + \
                      " <-- " + str(d_next['path']+"@"+str(d_next['revision'])).ljust(max_len) + "\x1b[0m"
    return ancestors

def get_rev_map(rev_map, src_rev, prefix):
    """
    Find the equivalent rev # in the target repo for the given rev # from the source repo.
    """
    # Find the highest entry less-than-or-equal-to src_rev
    for rev in range(src_rev+1, 1, -1):
        if debug:
            print prefix + "\x1b[32m" + ">> get_rev_map: rev="+str(rev)+"  in_rev_map="+str(rev in rev_map) + "\x1b[0m"
        if rev in rev_map:
            return rev_map[rev]
    # Else, we fell off the bottom of the rev_map. Ruh-roh...
    display_error("Internal Error: get_rev_map: Unable to find match rev_map entry for src_rev=" + src_rev)

def get_svn_dirlist(svn_path, svn_rev = ""):
    """
    Get a list of all the child contents (recusive) of the given folder path.
    """
    args = ["list", "--recursive"]
    path = svn_path
    if svn_rev:
        args += ["-r", str(svn_rev)]
        path += "@"+str(svn_rev)
    args += [path]
    paths = run_svn(args, False, True)
    paths = paths.strip("\n").split("\n") if len(paths)>1 else []
    return paths

def replay_svn_copyfrom(source_repos_url, source_url, path_offset, target_url, source_rev, \
                        copyfrom_path, copyfrom_rev, rev_map, is_dir = False, prefix = ""):
    """
    Given a source path and it's copy-from origin info, replay the necessary
    "svn copy" and "svn rm" commands to correctly track renames across copy-from's.

    For example, consider a sequence of events like this:
    1. svn copy /trunk /branches/fix1
    2. (Make some changes on /branches/fix1)
    3. svn mv /branches/fix1/Proj1 /branches/fix1/Proj2  " Rename folder
    4. svn mv /branches/fix1/Proj2/file1.txt /branches/fix1/Proj2/file2.txt  " Rename file inside renamed folder
    5. svn co /trunk && svn merge /branches/fix1
    After the merge and commit, "svn log -v" with show a delete of /trunk/Proj1
    and and add of /trunk/Proj2 copy-from /branches/fix1/Proj2. If we were just
    to do a straight "svn export+add" based on the /branches/fix1/Proj2 folder,
    we'd lose the logical history that Proj2/file2.txt is really a descendant
    of Proj1/file1.txt.

    'source_repos_url' is the full URL to the root of the source repository.
    'source_url' is the full URL to the source path in the source repository.
    'path_offset' is the offset from source_base to the file to check ancestry for,
      e.g. 'projectA/file1.txt'. path = source_repos_url + source_base + path_offset.
    'target_url' is the full URL to the target path in the target repository.
    'source_rev' is the revision ("svn log") that we're processing from the source repo.
    'copyfrom_path' is copy-from path, e.g. '/branches/bug123/projectA/file1.txt'
    'copyfrom_rev' is revision this copy-from path was copied at.
    'rev_map' is the running mapping-table dictionary for source-repo rev #'s
      to the equivalent target-repo rev #'s.
    'is_dir' is whether path_offset is a directory (rather than a file).
    """
    source_base = source_url[len(source_repos_url):]
    srcfrom_path = copyfrom_path
    srcfrom_rev =  copyfrom_rev
    if debug:
        print prefix + "\x1b[32m" + ">> replay_svn_copyfrom: Check copy-from: " + source_base+" "+path_offset + " --> " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
    if source_base in copyfrom_path:
        # The copy-from path is inside source_base, no need to check ancestry.
        if debug:
            print prefix + "\x1b[32;1m" + ">> replay_svn_copyfrom: Check copy-from: Found copy (in source_base): " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
    else:
        # Check if the copy-from path has ancestors which chain back to the current source_base
        ancestors = find_svn_ancestors(source_repos_url, source_url, path_offset, source_rev,
                                       copyfrom_path, copyfrom_rev, prefix+"  ")
        if ancestors:
            # The copy-from path has ancestory back to source_url.
            # ancestors[n] is the original (pre-branch-copy) trunk path.
            # ancestors[n-1] is the first commit on the new branch.
            copyfrom_path = ancestors[len(ancestors)-1]['path']
            copyfrom_rev =  ancestors[len(ancestors)-1]['revision']
            if debug:
                print prefix + "\x1b[32;1m" + ">> replay_svn_copyfrom: Check copy-from: Found parent: " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
    if not source_base in copyfrom_path:
        # If this copy-from path has no ancestry back to source_url, then can't do a "svn copy".
        # Create (parent) directory if needed
        p_path = path_offset if is_dir else os.path.dirname(path_offset).strip() or '.'
        if not os.path.exists(p_path):
            os.makedirs(p_path)
        # Export the entire added tree.
        run_svn(["export", "--force", "-r", str(copyfrom_rev),
                 source_repos_url + copyfrom_path+"@"+str(copyfrom_rev), path_offset])
        if not in_svn(path_offset):
            run_svn(["add", "--parents", path_offset])
        # TODO: Need to copy SVN properties from source repos
    else:
        copyfrom_offset = copyfrom_path[len(source_base):].strip('/')
        if debug:
            print prefix + "\x1b[32m" + ">> replay_svn_copyfrom: svn_copy: Copy-from: " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
        # Copy this path from the equivalent path+rev in the target repo, to create the
        # equivalent history.
        tgt_rev = get_rev_map(rev_map, copyfrom_rev, prefix+"  ")
        if debug:
            print prefix + "\x1b[32m" + ">> replay_svn_copyfrom: get_rev_map: " + str(copyfrom_rev) + " (source) -> " + str(tgt_rev) + " (target)" + "\x1b[0m"
        run_svn(["copy", "-r", tgt_rev, target_url+"/"+copyfrom_offset+"@"+str(tgt_rev), path_offset])
        # Update the content in this fresh copy to match the final target revision.
        if is_dir:
            paths_local =  get_svn_dirlist(path_offset)
            paths_remote = get_svn_dirlist(source_url+"/"+path_offset, source_rev)
            if debug:
                print prefix + "\x1b[32m" + "paths_local:  " + str(paths_local) + "\x1b[0m"
                print prefix + "\x1b[32m" + "paths_remote: " + str(paths_remote) + "\x1b[0m"
            # Update files/folders which exist in remote but not local
            for path in paths_remote:
                if not path in paths_local:
                    path_is_dir = True if path[-1] == "/" else False
                    replay_svn_copyfrom(source_repos_url, source_url, path_offset+"/"+path,
                                        target_url, source_rev,
                                        srcfrom_path+"/"+path, srcfrom_rev,
                                        rev_map, path_is_dir, prefix+"  ")
            # Remove files/folders which exist in local but not remote
            for path in paths_local:
                if not path in paths_remote:
                    if svnlog_verbose:
                        print " D " + source_base+"/"+path_offset+"/"+path
                    run_svn(["remove", "--force", path_offset+"/"+path])
                    # TODO: Does this handle deleted folders too? Wouldn't want to have a case
                    #       where we only delete all files from folder but leave orphaned folder around.
        else:
            run_svn(["export", "--force", "-r", str(source_rev),
                     source_repos_url+source_base+"/"+path_offset+"@"+str(source_rev), path_offset])

def process_svn_log_entry(log_entry, source_repos_url, source_url, target_url, \
                          rev_map, removed_paths = [], commit_paths = [], prefix = ""):
    """
    Process SVN changes from the given log entry.
    Returns array of all the paths in the working-copy that were changed,
    i.e. the paths which need to be "svn commit".

    'log_entry' is the array structure built by parse_svn_log_xml().
    'source_repos_url' is the full URL to the root of the source repository.
    'source_url' is the full URL to the source path in the source repository.
    'target_url' is the full URL to the target path in the target repository.
    'rev_map' is the running mapping-table dictionary for source-repo rev #'s
      to the equivalent target-repo rev #'s.
    'removed_paths' is the working list of deferred deletions.
    'commit_paths' is the working list of specific paths which changes to pass
      to the final "svn commit".
    """
    # Get the relative offset of source_url based on source_repos_url
    # e.g. '/branches/bug123'
    source_base = source_url[len(source_repos_url):]
    source_rev = log_entry['revision']
    if debug:
        print prefix + "\x1b[32m" + ">> process_svn_log_entry: " + source_url+"@"+str(source_rev) + "\x1b[0m"
    for d in log_entry['changed_paths']:
        # Get the full path for this changed_path
        # e.g. '/branches/bug123/projectA/file1.txt'
        path = d['path']
        if not path.startswith(source_base + "/"):
            # Ignore changed files that are not part of this subdir
            if path != source_base:
                if debug:
                    print prefix + "\x1b[90m" + ">> process_svn_log_entry: Unrelated path: " + path + "  (" + source_base + ")" + "\x1b[0m"
            continue
        # Calculate the offset (based on source_base) for this changed_path
        # e.g. 'projectA/file1.txt'
        # (path = source_base + "/" + path_offset)
        path_offset = path[len(source_base):].strip("/")
        # Get the action for this path
        action = d['action']
        if action not in 'MARD':
            display_error("In SVN rev. %d: action '%s' not supported. \
                           Please report a bug!" % (source_rev, action))

        # Try to be efficient and keep track of an explicit list of paths in the
        # working copy that changed. If we commit from the root of the working copy,
        # then SVN needs to crawl the entire working copy looking for pending changes.
        # But, if we gather too many paths to commit, then we wipe commit_paths below
        # and end-up doing a commit at the root of the working-copy.
        if len (commit_paths) < 100:
            commit_paths.append(path_offset)

        # Special-handling for replace's
        if action == 'R':
            if svnlog_verbose:
                msg = " " + action + " " + d['path']
                if d['copyfrom_path']:
                    msg += " (from " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + ")"
                print prefix + msg
            # If file was "replaced" (deleted then re-added, all in same revision),
            # then we need to run the "svn rm" first, then change action='A'. This
            # lets the normal code below handle re-"svn add"'ing the files. This
            # should replicate the "replace".
            run_svn(["remove", "--force", path_offset])
            action = 'A'

        # Handle all the various action-types
        # (Handle "add" first, for "svn copy/move" support)
        if action == 'A':
            if svnlog_verbose:
                msg = " " + action + " " + d['path']
                if d['copyfrom_path']:
                    msg += " (from " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + ")"
                print prefix + msg
            # If we have any queued deletions for this same path, remove those if we're re-adding this path.
            if (path_offset) in removed_paths:
                removed_paths.remove(path_offset)
            # Determine where to export from.
            copyfrom_path = path
            copyfrom_rev =  source_rev
            svn_copy = False
            path_is_dir = True if d['kind'] == 'dir' else False
            # Handle cases where this "add" was a copy from another URL in the source repos
            if d['copyfrom_revision']:
                copyfrom_path = d['copyfrom_path']
                copyfrom_rev =  d['copyfrom_revision']
                replay_svn_copyfrom(source_repos_url, source_url, path_offset, target_url, source_rev,
                                    copyfrom_path, copyfrom_rev, rev_map, path_is_dir, prefix+"  ")
            # Else just "svn export" the files from the source repo and "svn add" them.
            else:
                # Create (parent) directory if needed
                p_path = path_offset if path_is_dir else os.path.dirname(path_offset).strip() or '.'
                if not os.path.exists(p_path):
                    os.makedirs(p_path)
                # Export the entire added tree.
                run_svn(["export", "--force", "-r", str(copyfrom_rev),
                         source_repos_url + copyfrom_path+"@"+str(copyfrom_rev), path_offset])
                if not in_svn(path_offset):
                    run_svn(["add", "--parents", path_offset])
                # TODO: Need to copy SVN properties from source repos

        elif action == 'D':
            # Queue "svn remove" commands, to allow the action == 'A' handling the opportunity
            # to do smart "svn copy" handling on copy/move/renames.
            if not (path_offset) in removed_paths:
                removed_paths.append(path_offset)

        elif action == 'M':
            if svnlog_verbose:
                print prefix + " " + action + " " + d['path']
            # TODO: Is "svn merge -c" correct here? Should this just be an "svn export" plus
            #       proplist updating?
            out = run_svn(["merge", "-c", str(source_rev), "--non-recursive",
                     "--non-interactive", "--accept=theirs-full",
                     source_url+"/"+path_offset+"@"+str(source_rev), path_offset])

        else:
            display_error("Internal Error: process_svn_log_entry: Unhandled 'action' value: '" + action + "'")

    return commit_paths

def disp_svn_log_summary(log_entry):
    print "\n(Starting source rev #"+str(log_entry['revision'])+":)"
    print "r"+str(log_entry['revision']) + " | " + \
          log_entry['author'] + " | " + \
          str(datetime.fromtimestamp(int(log_entry['date'])).isoformat(' '))
    print log_entry['message']
    print "------------------------------------------------------------------------"

def pull_svn_rev(log_entry, source_repos_url, source_repos_uuid, source_url, target_url, rev_map, keep_author=False):
    """
    Pull SVN changes from the given log entry.
    Returns the new SVN revision.
    If an exception occurs, it will rollback to revision 'source_rev - 1'.
    """
    disp_svn_log_summary(log_entry)
    source_rev = log_entry['revision']

    # Process all the paths in this log entry
    removed_paths = []
    commit_paths = []
    process_svn_log_entry(log_entry, source_repos_url, source_url, target_url,
                          rev_map, removed_paths, commit_paths)
    # Process any deferred removed actions
    if removed_paths:
        path_base = source_url[len(source_repos_url):]
        for path_offset in removed_paths:
            if svnlog_verbose:
                print " D " + path_base+"/"+path_offset
            run_svn(["remove", "--force", path_offset])

    # If we had too many individual paths to commit, wipe the list and just commit at
    # the root of the working copy.
    if len (commit_paths) > 99:
        commit_paths = []

    # Add source-tracking revprop's
    revprops = [{'name':'source_uuid', 'value':source_repos_uuid},
                {'name':'source_url',  'value':source_url},
                {'name':'source_rev',  'value':source_rev}]
    commit_from_svn_log_entry(log_entry, commit_paths, keep_author=keep_author, revprops=revprops)
    print "(Finished source rev #"+str(source_rev)+")"

def main():
    usage = "Usage: %prog [-a] [-c] [-r SVN rev] <Source SVN URL> <Target SVN URL>"
    parser = OptionParser(usage)
    parser.add_option("-a", "--keep-author", action="store_true", dest="keep_author",
                      help="maintain original Author info from source repo")
    parser.add_option("-c", "--continue", action="store_true", dest="cont_from_break",
                      help="continue from previous break")
    parser.add_option("-r", type="int", dest="svn_rev",
                      help="initial SVN revision to checkout from")
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose",
                      help="display 'svn status'-like info for each action+changed-path being replayed")
    parser.add_option("--debug-showcmds", action="store_true", dest="debug_showcmds",
                      help="display each SVN command being executed")
    parser.add_option("--debug-debugmsgs", action="store_true", dest="debug_debugmsgs",
                      help="display debug messages")
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
    # Get the base URL for the source repos, e.g. 'svn://svn.example.com/svn/repo'
    source_repos_url = svn_info['repos_url']
    # Get the UUID for the source repos
    source_repos_uuid = svn_info['repos_uuid']

    dup_wc = "_dup_wc"
    rev_map = {}
    global debug
    global runsvn_showcmd
    global svnlog_verbose

    if options.debug_debugmsgs:
        debug = True
    if options.debug_showcmds:
        runsvn_showcmd = True
    if options.verbose:
        svnlog_verbose = True

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
        source_rev = svn_start_log['revision']

        # Check out a working copy of target_url
        dup_wc = os.path.abspath(dup_wc)
        if os.path.exists(dup_wc):
            shutil.rmtree(dup_wc)
        svn_checkout(target_url, dup_wc)
        os.chdir(dup_wc)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        paths = run_svn(["list", "-r", str(source_rev), source_url+"@"+str(source_rev)])
        if len(paths)>1:
            disp_svn_log_summary(get_one_svn_log_entry(source_url, source_rev, source_rev))
            print "(Initial import)"
            paths = paths.strip("\n").split("\n")
            for path in paths:
                # For each top-level file/folder...
                if not path:
                    # Skip null lines
                    break
                # Directories have a trailing slash in the "svn list" output
                path_is_dir = True if path[-1] == "/" else False
                if path_is_dir:
                    path=path.rstrip('/')
                    if not os.path.exists(path):
                        os.makedirs(path)
                run_svn(["export", "--force", "-r" , str(source_rev), source_url+"/"+path+"@"+str(source_rev), path])
                run_svn(["add", path])
            revprops = [{'name':'source_uuid', 'value':source_repos_uuid},
                        {'name':'source_url',  'value':source_url},
                        {'name':'source_rev',  'value':source_rev}]
            commit_from_svn_log_entry(svn_start_log, [], keep_author=keep_author, revprops=revprops)
            print "(Finished source rev #"+str(source_rev)+")"
    else:
        dup_wc = os.path.abspath(dup_wc)
        os.chdir(dup_wc)
        # TODO: Need better resume support. For the time being, expect caller explictly passes in resume revision.
        source_rev = options.svn_rev
        if source_rev < 1:
            display_error("Invalid arguments\n\nNeed to pass result rev # (-r) when using continue-mode (-c)", False)

    # Load SVN log starting from source_rev + 1
    it_log_entries = iter_svn_log_entries(source_url, source_rev + 1, greatest_rev)

    try:
        for log_entry in it_log_entries:
            # Replay this revision from source_url into target_url
            pull_svn_rev(log_entry, source_repos_url, source_repos_uuid, source_url,
                         target_url, rev_map, keep_author)
            # Update our target working-copy, to ensure everything says it's at the new HEAD revision
            run_svn(["up", dup_wc])
            # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
            dup_info = get_svn_info(target_url)
            dup_rev = dup_info['revision']
            source_rev = log_entry['revision']
            rev_map[source_rev] = dup_rev

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
