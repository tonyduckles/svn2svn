"""
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

License: GPLv3, same as hgsvn (https://bitbucket.org/andialbrecht/hgsvn)
Author: Tony Duckles (https://github.com/tonyduckles/svn2svn)
(Inspired by http://code.google.com/p/svn2svn/, and uses code for hgsvn
 for SVN client handling)
"""

from .. import base_version, full_version
from .. import ui
from .. import svnclient
from ..shell import run_svn
from ..errors import (ExternalCommandFailed, UnsupportedSVNAction, InternalError, VerificationError)
from parse import HelpFormatter

import sys
import os
import time
import traceback
import shutil
import operator
import optparse
from datetime import datetime

_valid_svn_actions = "MARD"   # The list of known SVN action abbr's, from "svn log"

def commit_from_svn_log_entry(log_entry, options, commit_paths=None, target_revprops=None):
    """
    Given an SVN log entry and an optional list of changed paths, do an svn commit.
    """
    # TODO: Run optional external shell hook here, for doing pre-commit filtering
    # Display the _wc_target "svn status" info if running in -vv (or higher) mode
    if ui.get_level() >= ui.EXTRA:
        ui.status(">> commit_from_svn_log_entry: Pre-commit _wc_target status:", level=ui.EXTRA, color='CYAN')
        ui.status(run_svn(["status"]), level=ui.EXTRA, color='CYAN')
    # This will use the local timezone for displaying commit times
    timestamp = int(log_entry['date'])
    svn_date = str(datetime.fromtimestamp(timestamp))
    # Uncomment this one one if you prefer UTC commit times
    #svn_date = "%d 0" % timestamp
    args = ["commit", "--force-log"]
    if options.keep_author:
        args += ["-m", log_entry['message'] + "\nDate: " + svn_date, "--username", log_entry['author']]
    else:
        args += ["-m", log_entry['message'] + "\nDate: " + svn_date + "\nAuthor: " + log_entry['author']]
    revprops = {}
    if log_entry['revprops']:
        # Carry forward any revprop's from the source revision
        for v in log_entry['revprops']:
            revprops[v['name']] = v['value']
    if target_revprops:
        # Add any extra revprop's we want to set for the target repo commits
        for v in target_revprops:
            revprops[v['name']] = v['value']
    if revprops:
        for key in revprops:
            args += ["--with-revprop", "%s=%s" % (key, str(revprops[key]))]
    if commit_paths:
        if len(commit_paths)<100:
            # If we don't have an excessive amount of individual changed paths, pass
            # those to the "svn commit" command. Else, pass nothing so we commit at
            # the root of the working-copy.
            args += list(commit_paths)
    rev = None
    if not options.dry_run:
        # Run the "svn commit" command, and screen-scrape the target_rev value (if any)
        output = run_svn(args)
        if output:
            output_lines = output.strip("\n").split("\n")
            rev = ""
            for line in output_lines:
                if line[0:19] == 'Committed revision ':
                    rev = line[19:].rstrip('.')
                    break
            if rev:
                ui.status("Committed revision %s.", rev)
    return rev

def full_svn_revert():
    """
    Do an "svn revert" and proactively remove any extra files in the working copy.
    """
    run_svn(["revert", "--recursive", "."])
    output = run_svn(["status"])
    if output:
        output_lines = output.strip("\n").split("\n")
        for line in output_lines:
            if line[0] == "?":
                path = line[4:].strip(" ")
                if os.path.isfile(path):
                    os.remove(path)
                if os.path.isdir(path):
                    shutil.rmtree(path)

def gen_tracking_revprops(source_repos_uuid, source_url, source_rev):
    """
    Build an array of svn2svn-specific source-tracking revprops.
    """
    revprops = [{'name':'svn2svn:source_uuid', 'value':source_repos_uuid},
                {'name':'svn2svn:source_url',  'value':source_url},
                {'name':'svn2svn:source_rev',  'value':source_rev}]
    return revprops

def in_svn(p, require_in_repo=False, prefix=""):
    """
    Check if a given file/folder is being tracked by Subversion.
    Prior to SVN 1.6, we could "cheat" and look for the existence of ".svn" directories.
    With SVN 1.7 and beyond, WC-NG means only a single top-level ".svn" at the root of the working-copy.
    Use "svn status" to check the status of the file/folder.
    """
    entries = svnclient.get_svn_status(p, no_recursive=True)
    if not entries:
        return False
    d = entries[0]
    if require_in_repo and (d['status'] == 'added' or d['revision'] is None):
        # If caller requires this path to be in the SVN repo, prevent returning True
        # for paths that are only locally-added.
        ret = False
    else:
        # Don't consider files tracked as deleted in the WC as under source-control.
        # Consider files which are locally added/copied as under source-control.
        ret = True if not (d['status'] == 'deleted') and (d['type'] == 'normal' or d['status'] == 'added' or d['copied'] == 'true') else False
    ui.status(prefix + ">> in_svn('%s', require_in_repo=%s) --> %s", p, str(require_in_repo), str(ret), level=ui.DEBUG, color='GREEN')
    return ret

def find_svn_ancestors(svn_repos_url, base_path, source_path, source_rev, prefix = ""):
    """
    Given a source path, walk the SVN history backwards to inspect the ancestory of
    that path, seeing if it traces back to base_path.  Build an array of copyfrom_path
    and copyfrom_revision pairs for each of the "svn copies". If we find a copyfrom_path
    which base_path is a substring match of (e.g. we crawled back to the initial branch-
    copy from trunk), then return the collection of ancestor paths.  Otherwise,
    copyfrom_path has no ancestory compared to base_path.

    This is useful when comparing "trunk" vs. "branch" paths, to handle cases where a
    file/folder was renamed in a branch and then that branch was merged back to trunk.

    'svn_repos_url' is the full URL to the root of the SVN repository,
      e.g. 'file:///path/to/repo'
    'base_path' is the path in the SVN repo to the target path we're trying to
      trace ancestry back to, e.g. 'trunk'.
    'source_path' is the path in the SVN repo to the source path to start checking
      ancestry at, e.g. 'branches/fix1/projectA/file1.txt'.
      (full_path = svn_repos_url+base_path+"/"+path_offset)
    'source_rev' is the revision to start walking the history of source_path backwards from.
    """
    ui.status(prefix + ">> find_svn_ancestors: Start: (%s) source_path: %s  base_path: %s",
        svn_repos_url, source_path+"@"+str(source_rev), base_path, level=ui.DEBUG, color='YELLOW')
    done = False
    working_path = base_path+"/"+source_path
    working_rev  = source_rev
    first_iter_done = False
    ancestors_temp = []
    while not done:
        # Get the first "svn log" entry for this path (relative to @rev)
        ui.status(prefix + ">> find_svn_ancestors: %s", svn_repos_url + working_path+"@"+str(working_rev), level=ui.DEBUG, color='YELLOW')
        log_entry = svnclient.get_first_svn_log_entry(svn_repos_url + working_path+"@"+str(working_rev), 1, working_rev, True)
        if not log_entry:
            ui.status(prefix + ">> find_svn_ancestors: Done: no log_entry", level=ui.DEBUG, color='YELLOW')
            done = True
            break
        # If we found a copy-from case which matches our base_path, we're done.
        # ...but only if we've at least tried to search for the first copy-from path.
        if first_iter_done and working_path.startswith(base_path):
            ui.status(prefix + ">> find_svn_ancestors: Done: Found working_path.startswith(base_path) and first_iter_done=True", level=ui.DEBUG, color='YELLOW')
            done = True
            break
        first_iter_done = True
        # Search for any actions on our target path (or parent paths).
        changed_paths_temp = []
        for d in log_entry['changed_paths']:
            path = d['path']
            if path in working_path:
                changed_paths_temp.append({'path': path, 'data': d})
        if not changed_paths_temp:
            # If no matches, then we've hit the end of the chain and this path has no ancestry back to base_path.
            ui.status(prefix + ">> find_svn_ancestors: Done: No matching changed_paths", level=ui.DEBUG, color='YELLOW')
            done = True
            continue
        # Reverse-sort any matches, so that we start with the most-granular (deepest in the tree) path.
        changed_paths = sorted(changed_paths_temp, key=operator.itemgetter('path'), reverse=True)
        # Find the action for our working_path in this revision. Use a loop to check in reverse order,
        # so that if the target file/folder is "M" but has a parent folder with an "A" copy-from.
        for v in changed_paths:
            d = v['data']
            path = d['path']
            # Check action-type for this file
            action = d['action']
            if action not in _valid_svn_actions:
                raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                    % (log_entry['revision'], action))
            ui.status(prefix + "> %s %s%s", action, path,
                (" (from %s)" % (d['copyfrom_path']+"@"+str(d['copyfrom_revision']))) if d['copyfrom_path'] else "",
                level=ui.DEBUG, color='YELLOW')
            if action == 'D':
                # If file/folder was deleted, it has no ancestor
                ancestors_temp = []
                ui.status(prefix + ">> find_svn_ancestors: Done: deleted", level=ui.DEBUG, color='YELLOW')
                done = True
                break
            if action in 'RA':
                # If file/folder was added/replaced but not a copy, it has no ancestor
                if not d['copyfrom_path']:
                    ancestors_temp = []
                    ui.status(prefix + ">> find_svn_ancestors: Done: %s with no copyfrom_path",
                        "Added" if action == "A" else "Replaced",
                        level=ui.DEBUG, color='YELLOW')
                    done = True
                    break
                # Else, file/folder was added/replaced and is a copy, so add an entry to our ancestors list
                # and keep checking for ancestors
                ui.status(prefix + ">> find_svn_ancestors: Found copy-from (action=%s): %s --> %s",
                    action, path, d['copyfrom_path']+"@"+str(d['copyfrom_revision']),
                    level=ui.DEBUG, color='YELLOW')
                ancestors_temp.append({'path': path, 'revision': log_entry['revision'],
                                       'copyfrom_path': d['copyfrom_path'], 'copyfrom_rev': d['copyfrom_revision']})
                working_path = working_path.replace(d['path'], d['copyfrom_path'])
                working_rev =  d['copyfrom_revision']
                # Follow the copy and keep on searching
                break
    ancestors = []
    if ancestors_temp:
        ancestors.append({'path': base_path+"/"+source_path, 'revision': source_rev})
        working_path = base_path+"/"+source_path
        for idx in range(len(ancestors_temp)):
            d = ancestors_temp[idx]
            working_path = working_path.replace(d['path'], d['copyfrom_path'])
            working_rev =  d['copyfrom_rev']
            ancestors.append({'path': working_path, 'revision': working_rev})
        if ui.get_level() >= ui.DEBUG:
            max_len = 0
            for idx in range(len(ancestors)):
                d = ancestors[idx]
                max_len = max(max_len, len(d['path']+"@"+str(d['revision'])))
            ui.status(prefix + ">> find_svn_ancestors: Found parent ancestors:", level=ui.DEBUG, color='YELLOW_B')
            for idx in range(len(ancestors)-1):
                d = ancestors[idx]
                d_next = ancestors[idx+1]
                ui.status(prefix + " [%s] %s <-- %s", idx,
                    str(d['path']+"@"+str(d['revision'])).ljust(max_len),
                    str(d_next['path']+"@"+str(d_next['revision'])).ljust(max_len),
                    level=ui.DEBUG, color='YELLOW')
    else:
        ui.status(prefix + ">> find_svn_ancestors: No ancestor-chain found: %s",
            svn_repos_url+base_path+"/"+source_path+"@"+str(source_rev), level=ui.DEBUG, color='YELLOW')
    return ancestors

def get_rev_map(rev_map, source_rev, prefix):
    """
    Find the equivalent rev # in the target repo for the given rev # from the source repo.
    """
    ui.status(prefix + ">> get_rev_map(%s)", source_rev, level=ui.DEBUG, color='GREEN')
    # Find the highest entry less-than-or-equal-to source_rev
    for rev in range(int(source_rev), 0, -1):
        ui.status(prefix + ">> get_rev_map: rev=%s  in_rev_map=%s", rev, str(rev in rev_map), level=ui.DEBUG, color='BLACK_B')
        if rev in rev_map:
            return int(rev_map[rev])
    # Else, we fell off the bottom of the rev_map. Ruh-roh...
    return None

def set_rev_map(rev_map, source_rev, target_rev):
    ui.status(">> set_rev_map: source_rev=%s target_rev=%s", source_rev, target_rev, level=ui.DEBUG, color='GREEN')
    rev_map[int(source_rev)]=int(target_rev)

def build_rev_map(target_url, source_info):
    """
    Check for any already-replayed history from source_url (source_info) and
    build the mapping-table of source_rev -> target_rev.
    """
    rev_map = {}
    ui.status("Rebuilding rev_map...", level=ui.VERBOSE)
    proc_count = 0
    it_log_entries = svnclient.iter_svn_log_entries(target_url, 1, 'HEAD', get_changed_paths=False, get_revprops=True)
    for log_entry in it_log_entries:
        if log_entry['revprops']:
            revprops = {}
            for v in log_entry['revprops']:
                if v['name'].startswith('svn2svn:'):
                    revprops[v['name']] = v['value']
            if revprops['svn2svn:source_uuid'] == source_info['repos_uuid'] and \
               revprops['svn2svn:source_url'] == source_info['url']:
                source_rev = revprops['svn2svn:source_rev']
                target_rev = log_entry['revision']
                set_rev_map(rev_map, source_rev, target_rev)
    return rev_map

def get_svn_dirlist(svn_path, svn_rev = ""):
    """
    Get a list of all the child contents (recusive) of the given folder path.
    """
    args = ["list"]
    path = svn_path
    if svn_rev:
        args += ["-r", svn_rev]
        path += "@"+str(svn_rev)
    args += [path]
    paths = run_svn(args, no_fail=True)
    paths = paths.strip("\n").split("\n") if len(paths)>1 else []
    return paths

def path_in_list(paths, path):
    for p in paths:
        if path.startswith(p):
            return True
    return False

def add_path(paths, path):
    if not path_in_list(paths, path):
        paths.append(path)

def do_svn_add(source_repos_url, source_url, path_offset, target_url, source_rev, \
               parent_copyfrom_path="", parent_copyfrom_rev="", export_paths={}, \
               rev_map={}, is_dir = False, prefix = ""):
    """
    Given the add'd source path, replay the "svn add/copy" commands to correctly
    track renames across copy-from's.

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
    'parent_copyfrom_path' and 'parent_copyfrom_rev' is the copy-from path of the parent
      directory, when being called recursively by do_svn_add_dir().
    'export_paths' is the list of path_offset's that we've deferred running "svn export" on.
    'rev_map' is the running mapping-table dictionary for source-repo rev #'s
      to the equivalent target-repo rev #'s.
    'is_dir' is whether path_offset is a directory (rather than a file).
    """
    source_base = source_url[len(source_repos_url):]
    ui.status(prefix + ">> do_svn_add: %s  %s", source_base+"/"+path_offset+"@"+str(source_rev),
        "  (parent-copyfrom: "+parent_copyfrom_path+"@"+str(parent_copyfrom_rev)+")" if parent_copyfrom_path else "",
        level=ui.DEBUG, color='GREEN')
    # Check if the given path has ancestors which chain back to the current source_base
    found_ancestor = False
    ancestors = find_svn_ancestors(source_repos_url, source_base, path_offset, source_rev, prefix+"  ")
    # ancestors[n] is the original (pre-branch-copy) trunk path.
    # ancestors[n-1] is the first commit on the new branch.
    copyfrom_path = ancestors[len(ancestors)-1]['path']     if ancestors else ""
    copyfrom_rev  = ancestors[len(ancestors)-1]['revision'] if ancestors else ""
    if ancestors:
        # The copy-from path has ancestory back to source_url.
        ui.status(prefix + ">> do_svn_add: Check copy-from: Found parent: %s", copyfrom_path+"@"+str(copyfrom_rev),
            level=ui.DEBUG, color='GREEN', bold=True)
        found_ancestor = True
        # Map the copyfrom_rev (source repo) to the equivalent target repo rev #. This can
        # return None in the case where copyfrom_rev is *before* our source_start_rev.
        tgt_rev = get_rev_map(rev_map, copyfrom_rev, prefix+"  ")
        ui.status(prefix + ">> do_svn_add: get_rev_map: %s (source) -> %s (target)", copyfrom_rev, tgt_rev, level=ui.DEBUG, color='GREEN')
    else:
        ui.status(prefix + ">> do_svn_add: Check copy-from: No ancestor chain found.", level=ui.DEBUG, color='GREEN')
        found_ancestor = False
    if found_ancestor and tgt_rev:
        # Check if this path_offset in the target WC already has this ancestry, in which
        # case there's no need to run the "svn copy" (again).
        path_in_svn = in_svn(path_offset, prefix=prefix+"  ")
        log_entry = svnclient.get_last_svn_log_entry(path_offset, 1, 'HEAD', get_changed_paths=False) if in_svn(path_offset, require_in_repo=True, prefix=prefix+"  ") else []
        if (not log_entry or (log_entry['revision'] != tgt_rev)):
            copyfrom_offset = copyfrom_path[len(source_base):].strip('/')
            ui.status(prefix + ">> do_svn_add: svn_copy: Copy-from: %s", copyfrom_path+"@"+str(copyfrom_rev), level=ui.DEBUG, color='GREEN')
            ui.status(prefix + "   copyfrom: %s", copyfrom_path+"@"+str(copyfrom_rev), level=ui.DEBUG, color='GREEN')
            ui.status(prefix + " p_copyfrom: %s", parent_copyfrom_path+"@"+str(parent_copyfrom_rev) if parent_copyfrom_path else "", level=ui.DEBUG, color='GREEN')
            if path_in_svn and \
               ((parent_copyfrom_path and copyfrom_path.startswith(parent_copyfrom_path)) and \
                (parent_copyfrom_rev and copyfrom_rev == parent_copyfrom_rev)):
                # When being called recursively, if this child entry has the same ancestor as the
                # the parent, then no need to try to run another "svn copy".
                ui.status(prefix + ">> do_svn_add: svn_copy: Same ancestry as parent: %s",
                    parent_copyfrom_path+"@"+str(parent_copyfrom_rev),level=ui.DEBUG, color='GREEN')
                pass
            else:
                # Copy this path from the equivalent path+rev in the target repo, to create the
                # equivalent history.
                if parent_copyfrom_path:
                    # If we have a parent copy-from path, we mis-match that so display a status
                    # message describing the action we're mimic'ing. If path_in_svn, then this
                    # is logically a "replace" rather than an "add".
                    ui.status(" %s %s (from %s)", ('R' if path_in_svn else 'A'), source_base+"/"+path_offset, ancestors[1]['path']+"@"+str(copyfrom_rev), level=ui.VERBOSE)
                if path_in_svn:
                    # If local file is already under version-control, then this is a replace.
                    ui.status(prefix + ">> do_svn_add: pre-copy: local path already exists: %s", path_offset, level=ui.DEBUG, color='GREEN')
                    run_svn(["remove", "--force", path_offset])
                run_svn(["copy", "-r", tgt_rev, target_url+"/"+copyfrom_offset+"@"+str(tgt_rev), path_offset])
                # Export the final version of this file/folder from the source repo, to make
                # sure we're up-to-date.
                add_path(export_paths, path_offset)
        else:
            ui.status(prefix + ">> do_svn_add: Skipped 'svn copy': %s", path_offset, level=ui.DEBUG, color='GREEN')
    else:
        # Else, either this copy-from path has no ancestry back to source_url OR copyfrom_rev comes
        # before our initial source_start_rev (i.e. tgt_rev == None), so can't do a "svn copy".
        # Create (parent) directory if needed.
        # TODO: This is (nearly) a duplicate of code in process_svn_log_entry(). Should this be
        #       split-out to a shared tag?
        p_path = path_offset if is_dir else os.path.dirname(path_offset).strip() or '.'
        if not os.path.exists(p_path):
            run_svn(["mkdir", p_path])
        if not in_svn(path_offset, prefix=prefix+"  "):
            if is_dir:
                # Export the final verison of all files in this folder.
                add_path(export_paths, path_offset)
            else:
                # Export the final verison of this file. We *need* to do this before running
                # the "svn add", even if we end-up re-exporting this file again via export_paths.
                run_svn(["export", "--force", "-r", source_rev,
                         source_repos_url+source_base+"/"+path_offset+"@"+str(source_rev), path_offset])
            # If not already under version-control, then "svn add" this file/folder.
            run_svn(["add", "--parents", path_offset])
        # TODO: Need to copy SVN properties from source repos
    if is_dir:
        # For any folders that we process, process any child contents, so that we correctly
        # replay copies/replaces/etc.
        do_svn_add_dir(source_repos_url, source_url, path_offset, source_rev, target_url,
                       copyfrom_path, copyfrom_rev, export_paths, rev_map, prefix+"  ")

def do_svn_add_dir(source_repos_url, source_url, path_offset, source_rev, target_url, \
                   parent_copyfrom_path, parent_copyfrom_rev, export_paths, rev_map, prefix=""):
    source_base = source_url[len(source_repos_url):]
    # Get the directory contents, to compare between the local WC (target_url) vs. the remote repo (source_url)
    # TODO: paths_local won't include add'd paths because "svn ls" lists the contents of the
    #       associated remote repo folder. (Is this a problem?)
    paths_local =  get_svn_dirlist(path_offset)
    paths_remote = get_svn_dirlist(source_url+"/"+path_offset, source_rev)
    ui.status(prefix + ">> do_svn_add_dir: paths_local:  %s", str(paths_local),  level=ui.DEBUG, color='GREEN')
    ui.status(prefix + ">> do_svn_add_dir: paths_remote: %s", str(paths_remote), level=ui.DEBUG, color='GREEN')
    # Update files/folders which exist in remote but not local
    for path in paths_remote:
        path_is_dir = True if path[-1] == "/" else False
        working_path = path_offset+"/"+(path.rstrip('/') if path_is_dir else path)
        do_svn_add(source_repos_url, source_url, working_path, target_url, source_rev,
                   parent_copyfrom_path, parent_copyfrom_rev, export_paths,
                   rev_map, path_is_dir, prefix+"  ")
    # Remove files/folders which exist in local but not remote
    for path in paths_local:
        if not path in paths_remote:
            ui.status(" %s %s", 'D', source_base+"/"+path_offset+"/"+path, level=ui.VERBOSE)
            run_svn(["remove", "--force", path_offset+"/"+path])
            # TODO: Does this handle deleted folders too? Wouldn't want to have a case
            #       where we only delete all files from folder but leave orphaned folder around.

def process_svn_log_entry(log_entry, source_repos_url, source_url, target_url, \
                          rev_map, options, commit_paths = [], prefix = ""):
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
    'commit_paths' is the working list of specific paths which changes to pass
      to the final "svn commit".
    """
    export_paths = []
    # Get the relative offset of source_url based on source_repos_url
    # e.g. '/branches/bug123'
    source_base = source_url[len(source_repos_url):]
    source_rev = log_entry['revision']
    ui.status(prefix + ">> process_svn_log_entry: %s", source_url+"@"+str(source_rev), level=ui.DEBUG, color='GREEN')
    for d in log_entry['changed_paths']:
        # Get the full path for this changed_path
        # e.g. '/branches/bug123/projectA/file1.txt'
        path = d['path']
        if not path.startswith(source_base + "/"):
            # Ignore changed files that are not part of this subdir
            if path != source_base:
                ui.status(prefix + ">> process_svn_log_entry: Unrelated path: %s  (base: %s)", path, source_base, level=ui.DEBUG, color='GREEN')
            continue
        assert len(d['kind'])>0
        path_is_dir = True if d['kind'] == 'dir' else False
        # Calculate the offset (based on source_base) for this changed_path
        # e.g. 'projectA/file1.txt'
        # (path = source_base + "/" + path_offset)
        path_offset = path[len(source_base):].strip("/")
        # Get the action for this path
        action = d['action']
        if action not in _valid_svn_actions:
            raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                % (source_rev, action))
        ui.status(" %s %s%s", action, d['path'],
            (" (from %s)" % (d['copyfrom_path']+"@"+str(d['copyfrom_revision']))) if d['copyfrom_path'] else "",
            level=ui.VERBOSE)

        # Try to be efficient and keep track of an explicit list of paths in the
        # working copy that changed. If we commit from the root of the working copy,
        # then SVN needs to crawl the entire working copy looking for pending changes.
        add_path(commit_paths, path_offset)

        # Special-handling for replace's
        if action == 'R':
            # If file was "replaced" (deleted then re-added, all in same revision),
            # then we need to run the "svn rm" first, then change action='A'. This
            # lets the normal code below handle re-"svn add"'ing the files. This
            # should replicate the "replace".
            run_svn(["remove", "--force", path_offset])
            action = 'A'

        # Handle all the various action-types
        # (Handle "add" first, for "svn copy/move" support)
        if action == 'A':
            # Determine where to export from.
            svn_copy = False
            # Handle cases where this "add" was a copy from another URL in the source repos
            if d['copyfrom_revision']:
                copyfrom_path = d['copyfrom_path']
                copyfrom_rev =  d['copyfrom_revision']
                do_svn_add(source_repos_url, source_url, path_offset, target_url, source_rev,
                           "", "", export_paths, rev_map, path_is_dir, prefix+"  ")
            # Else just "svn export" the files from the source repo and "svn add" them.
            else:
                # Create (parent) directory if needed
                p_path = path_offset if path_is_dir else os.path.dirname(path_offset).strip() or '.'
                if not os.path.exists(p_path):
                    run_svn(["mkdir", p_path])
                # Export the entire added tree.
                if path_is_dir:
                    # For directories, defer the (recurisve) "svn export". Might have a
                    # situation in a branch merge where the entry in the svn-log is a
                    # non-copy-from'd "add" but there are child contents (that we haven't
                    # gotten to yet in log_entry) that are copy-from's.  When we try do
                    # the "svn copy" later on in do_svn_add() for those copy-from'd paths,
                    # having pre-existing (svn-add'd) contents creates some trouble.
                    # Instead, just create the stub folders ("svn mkdir" above) and defer
                    # exporting the final file-state until the end.
                    add_path(export_paths, path_offset)
                else:
                    # Export the final verison of this file. We *need* to do this before running
                    # the "svn add", even if we end-up re-exporting this file again via export_paths.
                    run_svn(["export", "--force", "-r", source_rev,
                             source_url+"/"+path_offset+"@"+str(source_rev), path_offset])
                if not in_svn(path_offset, prefix=prefix+"  "):
                    # Need to use in_svn here to handle cases where client committed the parent
                    # folder and each indiv sub-folder.
                    run_svn(["add", "--parents", path_offset])
                # TODO: Need to copy SVN properties from source repos

        elif action == 'D':
            run_svn(["remove", "--force", path_offset])

        elif action == 'M':
            # TODO: Is "svn merge -c" correct here? Should this just be an "svn export" plus
            #       proplist updating?
            out = run_svn(["merge", "-c", source_rev, "--non-recursive",
                     "--non-interactive", "--accept=theirs-full",
                     source_url+"/"+path_offset+"@"+str(source_rev), path_offset])

        else:
            raise InternalError("Internal Error: process_svn_log_entry: Unhandled 'action' value: '%s'"
                % action)

    # Export the final version of all add'd paths from source_url
    if export_paths:
        for path_offset in export_paths:
            run_svn(["export", "--force", "-r", source_rev,
                     source_url+"/"+path_offset+"@"+str(source_rev), path_offset])

    return commit_paths

def disp_svn_log_summary(log_entry):
    ui.status("")
    ui.status("r%s | %s | %s",
        log_entry['revision'],
        log_entry['author'],
        str(datetime.fromtimestamp(int(log_entry['date'])).isoformat(' ')))
    ui.status(log_entry['message'])
    ui.status("------------------------------------------------------------------------")

def real_main(options, args):
    source_url = args.pop(0).rstrip("/")
    target_url = args.pop(0).rstrip("/")
    ui.status("options: %s", str(options), level=ui.DEBUG, color='GREEN')

    # Make sure that both the source and target URL's are valid
    source_info = svnclient.get_svn_info(source_url)
    assert source_url.startswith(source_info['repos_url'])
    target_info = svnclient.get_svn_info(target_url)
    assert target_url.startswith(target_info['repos_url'])

    source_end_rev = source_info['revision']       # Get the last revision # for the source repo
    source_repos_url = source_info['repos_url']    # Get the base URL for the source repo, e.g. 'svn://svn.example.com/svn/repo'
    source_repos_uuid = source_info['repos_uuid']  # Get the UUID for the source repo

    wc_target = os.path.abspath('_wc_target')
    rev_map = {}
    num_entries_proc = 0
    commit_count = 0
    source_rev = None
    target_rev = None

    # Check out a working copy of target_url if needed
    wc_exists = os.path.exists(wc_target)
    if wc_exists and not options.cont_from_break:
        shutil.rmtree(wc_target)
        wc_exists = False
    if not wc_exists:
        svnclient.svn_checkout(target_url, wc_target)
    os.chdir(wc_target)

    if not options.cont_from_break:
        # TODO: Warn user if trying to start (non-continue) into a non-empty target path?
        # Get log entry for the SVN revision we will check out
        if options.svn_rev:
            # If specify a rev, get log entry just before or at rev
            source_start_log = svnclient.get_last_svn_log_entry(source_url, 1, options.svn_rev, False)
        else:
            # Otherwise, get log entry of branch creation
            # Note: Trying to use svnclient.get_first_svn_log_entry(source_url, 1, source_end_rev, False)
            # ends-up being *VERY* time-consuming on a repo with lots of revisions. Even though
            # the "svn log" call is passing --limit 1, it seems like that limit-filter is happening
            # _after_ svn has fetched the full log history. Instead, search the history in chunks
            # and write some progress to the screen.
            ui.status("Searching for start source revision (%s)...", source_url, level=ui.VERBOSE)
            rev = 1
            chunk_size = 1000
            done = False
            while not done:
                entries = svnclient.run_svn_log(source_url, rev, min(rev+chunk_size-1, target_info['revision']), 1, get_changed_paths=False)
                if entries:
                    source_start_log = entries[0]
                    done = True
                    break
                ui.status("...%s...", rev)
                rev = rev+chunk_size
                if rev > target_info['revision']:
                    done = True
            if not source_start_log:
                raise InternalError("Unable to find first revision for source_url: %s" % source_url)

        # This is the revision we will start from for source_url
        source_start_rev = source_rev = int(source_start_log['revision'])
        ui.status("Starting at source revision %s.", source_start_rev, level=ui.VERBOSE)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        paths = run_svn(["list", "-r", source_rev, source_url+"@"+str(source_rev)])
        if len(paths)>1:
            disp_svn_log_summary(svnclient.get_one_svn_log_entry(source_url, source_rev, source_rev))
            ui.status("(Initial import)", level=ui.VERBOSE)
            paths = paths.strip("\n").split("\n")
            for path_raw in paths:
                # For each top-level file/folder...
                if not path_raw:
                    continue
                # Directories have a trailing slash in the "svn list" output
                path_is_dir = True if path_raw[-1] == "/" else False
                path = path_raw.rstrip('/') if path_is_dir else path_raw
                if path_is_dir and not os.path.exists(path):
                    os.makedirs(path)
                ui.status(" A %s", source_url[len(source_repos_url):]+"/"+path, level=ui.VERBOSE)
                run_svn(["export", "--force", "-r" , source_rev, source_url+"/"+path+"@"+str(source_rev), path])
                run_svn(["add", path])
            num_entries_proc += 1
            target_revprops = gen_tracking_revprops(source_repos_uuid, source_url, source_rev)   # Build source-tracking revprop's
            target_rev = commit_from_svn_log_entry(source_start_log, options, target_revprops=target_revprops)
            if target_rev:
                # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
                set_rev_map(rev_map, source_rev, target_rev)
                # Update our target working-copy, to ensure everything says it's at the new HEAD revision
                run_svn(["update"])
                commit_count += 1
    else:
        # Re-build the rev_map based on any already-replayed history in target_url
        rev_map = build_rev_map(target_url, source_info)
        if not rev_map:
            raise RuntimeError("Called with continue-mode, but no already-replayed history found in target repo: %s" % target_url)
        source_start_rev = int(max(rev_map, key=rev_map.get))
        assert source_start_rev
        ui.status("Continuing from source revision %s.", source_start_rev, level=ui.VERBOSE)

    svn_vers_t = svnclient.get_svn_client_version()
    svn_vers = float(".".join(map(str, svn_vers_t[0:2])))

    # Load SVN log starting from source_start_rev + 1
    it_log_entries = svnclient.iter_svn_log_entries(source_url, source_start_rev+1, source_end_rev, get_revprops=True)
    source_rev = None

    try:
        for log_entry in it_log_entries:
            if options.entries_proc_limit:
                if num_entries_proc >= options.entries_proc_limit:
                    break
            # Replay this revision from source_url into target_url
            disp_svn_log_summary(log_entry)
            source_rev = log_entry['revision']
            # Process all the changed-paths in this log entry
            commit_paths = []
            process_svn_log_entry(log_entry, source_repos_url, source_url, target_url,
                                  rev_map, options, commit_paths)
            num_entries_proc += 1
            # Commit any changes made to _wc_target
            target_revprops = gen_tracking_revprops(source_repos_uuid, source_url, source_rev)   # Build source-tracking revprop's
            target_rev = commit_from_svn_log_entry(log_entry, options, commit_paths, target_revprops=target_revprops)
            if target_rev:
                # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
                source_rev = log_entry['revision']
                set_rev_map(rev_map, source_rev, target_rev)
                # Update our target working-copy, to ensure everything says it's at the new HEAD revision
                run_svn(["update"])
                commit_count += 1
                # Run "svn cleanup" every 100 commits if SVN 1.7+, to clean-up orphaned ".svn/pristines/*"
                if svn_vers >= 1.7 and (commit_count % 100 == 0):
                    run_svn(["cleanup"])
        if not source_rev:
            # If there were no new source_url revisions to process, init source_rev
            # for the "finally" message below.
            source_rev = source_end_rev

    except KeyboardInterrupt:
        print "\nStopped by user."
        print "\nCleaning-up..."
        run_svn(["cleanup"])
        full_svn_revert()
    except:
        print "\nCommand failed with following error:\n"
        traceback.print_exc()
        print "\nCleaning-up..."
        run_svn(["cleanup"])
        print run_svn(["status"])
        full_svn_revert()
    finally:
        print "\nFinished at source revision %s." % source_rev

def main():
    # Defined as entry point. Must be callable without arguments.
    usage = "Usage: %prog [OPTIONS] source_url target_url"
    description = """\
  Replicate (replay) history from one SVN repository to another. Maintain
  logical ancestry wherever possible, so that 'svn log' on the replayed
  repo will correctly follow file/folder renames.

  == Examples ==
  Create a copy of only /trunk from source repo, starting at r5000
  $ svnadmin create /svn/target
  $ svn mkdir -m 'Add trunk' file:///svn/target/trunk
  $ svn2svn -av -r 5000 http://server/source/trunk file:///svn/target/trunk
    1. The target_url will be checked-out to ./_wc_target
    2. The first commit to http://server/source/trunk at/after r5000 will be
       exported & added into _wc_target
    3. All revisions affecting http://server/source/trunk (starting at r5000)
       will be replayed to _wc_target. Any add/copy/move/replaces that are
       copy-from'd some path outside of /trunk (e.g. files renamed on a
       /branch and branch was merged into /trunk) will correctly maintain
       logical ancestry where possible.

  Use continue-mode (-c) to pick-up where the last run left-off
  $ svn2svn -avc http://server/source/trunk file:///svn/target/trunk
    1. The target_url will be checked-out to ./_wc_target, if not already
       checked-out
    2. All new revisions affecting http://server/source/trunk starting from
       the last replayed revision to file:///svn/target/trunk (based on the
       svn2svn:* revprops) will be replayed to _wc_target, maintaining all
       logical ancestry where possible."""
    parser = optparse.OptionParser(usage, description=description,
                formatter=HelpFormatter(), version="%prog "+str(full_version))
    #parser.remove_option("--help")
    #parser.add_option("-h", "--help", dest="show_help", action="store_true",
    #    help="show this help message and exit")
    parser.add_option("-r", "--revision", type="int", dest="svn_rev", metavar="REV",
                      help="initial SVN revision to start source_url replay")
    parser.add_option("-a", "--keep-author", action="store_true", dest="keep_author", default=False,
                      help="maintain original 'Author' info from source repo")
    parser.add_option("-c", "--continue", action="store_true", dest="cont_from_break",
                      help="continue from previous break")
    parser.add_option("-l", "--limit", type="int", dest="entries_proc_limit", metavar="NUM",
                      help="maximum number of log entries to process")
    parser.add_option("-n", "--dry-run", action="store_true", dest="dry_run", default=False,
                      help="try processing next log entry but don't commit changes to "
                           "target working-copy (forces --limit=1)")
    parser.add_option("-v", "--verbose", dest="verbosity", action="count", default=1,
                      help="enable additional output (use -vv or -vvv for more)")
    parser.add_option("--debug", dest="verbosity", const=ui.DEBUG, action="store_const",
                      help="enable debugging output (same as -vvv)")
    options, args = parser.parse_args()
    if len(args) != 2:
        parser.error("incorrect number of arguments")
    if options.verbosity < 10:
        # Expand multiple "-v" arguments to a real ui._level value
        options.verbosity *= 10
    if options.dry_run:
        # When in dry-run mode, only try to process the next log_entry
        options.entries_proc_limit = 1
    ui.update_config(options)
    return real_main(options, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
