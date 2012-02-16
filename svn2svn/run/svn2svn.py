"""
Replicate (replay) changesets from one SVN repository to another.
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
import re
from datetime import datetime

_valid_svn_actions = "MARD"   # The list of known SVN action abbr's, from "svn log"

# Module-level variables/parameters
source_url = ""          # URL to source path in source SVN repo, e.g. 'http://server/svn/source/trunk'
source_repos_url = ""    # URL to root of source SVN repo,        e.g. 'http://server/svn/source'
source_base = ""         # Relative path of source_url in source SVN repo, e.g. '/trunk'
source_repos_uuid = ""   # UUID of source SVN repo
target_url =""           # URL to target path in target SVN repo, e.g. 'file:///svn/repo_target/trunk'
rev_map = {}             # The running mapping-table dictionary for source_url rev #'s -> target_url rev #'s
options = None           # optparser options

def parse_svn_commit_rev(output):
    """
    Parse the revision number from the output of "svn commit".
    """
    output_lines = output.strip("\n").split("\n")
    rev_num = None
    for line in output_lines:
        if line[0:19] == 'Committed revision ':
            rev_num = line[19:].rstrip('.')
            break
    assert rev_num is not None
    return int(rev_num)

def commit_from_svn_log_entry(log_entry, commit_paths=None, target_revprops=None):
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
    message = log_entry['message']
    if options.log_date:
        message += "\nDate: " + svn_date
    if options.log_author:
        message += "\nAuthor: " + log_entry['author']
    if options.keep_author:
        args += ["--username", log_entry['author']]
    args += ["-m", message]
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
    rev_num = None
    if not options.dry_run:
        # Run the "svn commit" command, and screen-scrape the target_rev value (if any)
        output = run_svn(args)
        rev_num = parse_svn_commit_rev(output) if output else None
        if rev_num is not None:
            ui.status("Committed revision %s.", rev_num)
            if options.keep_date:
                run_svn(["propset", "--revprop", "-r", rev_num, "svn:date", log_entry['date_raw']])
    return rev_num

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

def gen_tracking_revprops(source_rev):
    """
    Build an array of svn2svn-specific source-tracking revprops.
    """
    revprops = [{'name':'svn2svn:source_uuid', 'value':source_repos_uuid},
                {'name':'svn2svn:source_url',  'value':source_url},
                {'name':'svn2svn:source_rev',  'value':source_rev}]
    return revprops

def sync_svn_props(source_url, source_rev, path_offset):
    """
    Carry-forward any unversioned properties from the source repo to the
    target WC.
    """
    source_props = svnclient.get_all_props(source_url+"/"+path_offset,  source_rev)
    target_props = svnclient.get_all_props(path_offset)
    if 'svn:mergeinfo' in source_props:
        # Never carry-forward "svn:mergeinfo"
        del source_props['svn:mergeinfo']
    for prop in target_props:
        if prop not in source_props:
            # Remove any properties which exist in target but not source
            run_svn(["propdel", prop, path_offset])
    for prop in source_props:
        if prop not in target_props or \
           source_props[prop] != target_props[prop]:
            # Set/update any properties which exist in source but not target or
            # whose value differs between source vs. target.
            run_svn(["propset", prop, source_props[prop], path_offset])

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

def is_child_path(path, p_path):
    return True if (path == p_path) or (path.startswith(p_path+"/")) else False

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
      trace ancestry back to, e.g. '/trunk'.
    'source_path' is the path in the SVN repo to the source path to start checking
      ancestry at, e.g. '/branches/fix1/projectA/file1.txt'.
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
        log_entry = svnclient.get_first_svn_log_entry(svn_repos_url + working_path, 1, working_rev, True)
        if not log_entry:
            ui.status(prefix + ">> find_svn_ancestors: Done: no log_entry", level=ui.DEBUG, color='YELLOW')
            done = True
            break
        # If we found a copy-from case which matches our base_path, we're done.
        # ...but only if we've at least tried to search for the first copy-from path.
        if first_iter_done and is_child_path(working_path, base_path):
            ui.status(prefix + ">> find_svn_ancestors: Done: Found is_child_path(working_path, base_path) and first_iter_done=True", level=ui.DEBUG, color='YELLOW')
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

def get_rev_map(source_rev, prefix):
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

def set_rev_map(source_rev, target_rev):
    ui.status(">> set_rev_map: source_rev=%s target_rev=%s", source_rev, target_rev, level=ui.DEBUG, color='GREEN')
    global rev_map
    rev_map[int(source_rev)]=int(target_rev)

def build_rev_map(target_url, target_end_rev, source_info):
    """
    Check for any already-replayed history from source_url (source_info) and
    build the mapping-table of source_rev -> target_rev.
    """
    global rev_map
    rev_map = {}
    ui.status("Rebuilding target_rev -> source_rev rev_map...", level=ui.VERBOSE)
    proc_count = 0
    it_log_entries = svnclient.iter_svn_log_entries(target_url, 1, target_end_rev, get_changed_paths=False, get_revprops=True)
    for log_entry in it_log_entries:
        if log_entry['revprops']:
            revprops = {}
            for v in log_entry['revprops']:
                if v['name'].startswith('svn2svn:'):
                    revprops[v['name']] = v['value']
            if revprops and \
               revprops['svn2svn:source_uuid'] == source_info['repos_uuid'] and \
               revprops['svn2svn:source_url'] == source_info['url']:
                source_rev = revprops['svn2svn:source_rev']
                target_rev = log_entry['revision']
                set_rev_map(source_rev, target_rev)

def get_svn_dirlist(svn_path, rev_number = ""):
    """
    Get a list of all the child contents (recusive) of the given folder path.
    """
    args = ["list"]
    path = svn_path
    if rev_number:
        args += ["-r", rev_number]
        path += "@"+str(rev_number)
    args += [path]
    paths = run_svn(args, no_fail=True)
    paths = paths.strip("\n").split("\n") if len(paths)>1 else []
    return paths

def path_in_list(paths, path):
    for p in paths:
        if is_child_path(path, p):
            return True
    return False

def add_path(paths, path):
    if not path_in_list(paths, path):
        paths.append(path)

def do_svn_add(path_offset, source_rev, parent_copyfrom_path="", parent_copyfrom_rev="", \
               export_paths={}, is_dir = False, skip_paths=[], prefix = ""):
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

    'path_offset' is the offset from source_base to the file to check ancestry for,
      e.g. 'projectA/file1.txt'. path = source_repos_url + source_base + path_offset.
    'source_rev' is the revision ("svn log") that we're processing from the source repo.
    'parent_copyfrom_path' and 'parent_copyfrom_rev' is the copy-from path of the parent
      directory, when being called recursively by do_svn_add_dir().
    'export_paths' is the list of path_offset's that we've deferred running "svn export" on.
    'is_dir' is whether path_offset is a directory (rather than a file).
    """
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
        tgt_rev = get_rev_map(copyfrom_rev, prefix+"  ")
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
               ((parent_copyfrom_path and is_child_path(copyfrom_path, parent_copyfrom_path)) and \
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
                if is_dir:
                    # Export the final verison of all files in this folder.
                    add_path(export_paths, path_offset)
                else:
                    # Export the final verison of this file.
                    run_svn(["export", "--force", "-r", source_rev,
                             source_repos_url+source_base+"/"+path_offset+"@"+str(source_rev), path_offset])
                if options.keep_prop:
                    sync_svn_props(source_url, source_rev, path_offset)
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
        if options.keep_prop:
            sync_svn_props(source_url, source_rev, path_offset)
    if is_dir:
        # For any folders that we process, process any child contents, so that we correctly
        # replay copies/replaces/etc.
        do_svn_add_dir(path_offset, source_rev, copyfrom_path, copyfrom_rev, export_paths, skip_paths, prefix+"  ")

def do_svn_add_dir(path_offset, source_rev, parent_copyfrom_path, parent_copyfrom_rev, \
                   export_paths, skip_paths, prefix=""):
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
        if not working_path in skip_paths:
            do_svn_add(working_path, source_rev, parent_copyfrom_path, parent_copyfrom_rev,
                       export_paths, path_is_dir, skip_paths, prefix+"  ")
    # Remove files/folders which exist in local but not remote
    for path in paths_local:
        if not path in paths_remote:
            ui.status(" %s %s", 'D', source_base+"/"+path_offset+"/"+path, level=ui.VERBOSE)
            run_svn(["remove", "--force", path_offset+"/"+path])
            # TODO: Does this handle deleted folders too? Wouldn't want to have a case
            #       where we only delete all files from folder but leave orphaned folder around.

def process_svn_log_entry(log_entry, commit_paths, prefix = ""):
    """
    Process SVN changes from the given log entry. Build an array (commit_paths)
    of the paths in the working-copy that were changed, i.e. the paths which
    we'll pass to "svn commit".
    """
    export_paths = []
    source_rev = log_entry['revision']
    ui.status(prefix + ">> process_svn_log_entry: %s", source_url+"@"+str(source_rev), level=ui.DEBUG, color='GREEN')
    for d in log_entry['changed_paths']:
        # Get the full path for this changed_path
        # e.g. '/branches/bug123/projectA/file1.txt'
        path = d['path']
        if not is_child_path(path, source_base):
            # Ignore changed files that are not part of this subdir
            ui.status(prefix + ">> process_svn_log_entry: Unrelated path: %s  (base: %s)", path, source_base, level=ui.DEBUG, color='GREEN')
            continue
        if d['kind'] == "":
            # The "kind" value was introduced in SVN 1.6, and "svn log --xml" won't return a "kind"
            # value for commits made on a pre-1.6 repo, even if the server is now running 1.6.
            # We need to use other methods to fetch the node-kind for these cases.
            d['kind'] = svnclient.get_kind(source_repos_url, path, source_rev, d['action'], log_entry['changed_paths'])
        assert (d['kind'] == 'file') or (d['kind'] == 'dir')
        path_is_dir =  True if d['kind'] == 'dir'  else False
        path_is_file = True if d['kind'] == 'file' else False
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
        commit_paths.append(path_offset)

        # Special-handling for replace's
        if action == 'R':
            # If file was "replaced" (deleted then re-added, all in same revision),
            # then we need to run the "svn rm" first, then change action='A'. This
            # lets the normal code below handle re-"svn add"'ing the files. This
            # should replicate the "replace".
            if in_svn(path_offset):
                # Target path might not be under version-control yet, e.g. parent "add"
                # was a copy-from a branch which had no ancestry back to trunk, and each
                # child folder under that parent folder is a "replace" action on the final
                # merge to trunk. Since the child folders will be in skip_paths, do_svn_add
                # wouldn't have created them while processing the parent "add" path.
                run_svn(["remove", "--force", path_offset])
            action = 'A'

        # Handle all the various action-types
        # (Handle "add" first, for "svn copy/move" support)
        if action == 'A':
            # Determine where to export from.
            svn_copy = False
            # Handle cases where this "add" was a copy from another URL in the source repo
            if d['copyfrom_revision']:
                copyfrom_path = d['copyfrom_path']
                copyfrom_rev =  d['copyfrom_revision']
                skip_paths = []
                for tmp_d in log_entry['changed_paths']:
                    tmp_path = tmp_d['path']
                    if is_child_path(tmp_path, path):
                        # Build list of child entries which are also in the changed_paths list,
                        # so that do_svn_add() can skip processing these entries when recursing
                        # since we'll end-up processing them later.
                        tmp_path_offset = tmp_path[len(source_base):].strip("/")
                        skip_paths.append(tmp_path_offset)
                do_svn_add(path_offset, source_rev, "", "", export_paths, path_is_dir, skip_paths, prefix+"  ")
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
                if options.keep_prop:
                    sync_svn_props(source_url, source_rev, path_offset)

        elif action == 'D':
            run_svn(["remove", "--force", path_offset])

        elif action == 'M':
            if path_is_file:
                run_svn(["export", "--force", "-N" , "-r", source_rev,
                         source_url+"/"+path_offset+"@"+str(source_rev), path_offset])
            if options.keep_prop:
                sync_svn_props(source_url, source_rev, path_offset)

        else:
            raise InternalError("Internal Error: process_svn_log_entry: Unhandled 'action' value: '%s'"
                % action)

    # Export the final version of all add'd paths from source_url
    if export_paths:
        for path_offset in export_paths:
            run_svn(["export", "--force", "-r", source_rev,
                     source_url+"/"+path_offset+"@"+str(source_rev), path_offset])

def keep_revnum(source_rev, target_rev_last, wc_target_tmp):
    """
    Add "padding" target revisions as needed to keep source and target
    revision #'s identical.
    """
    if int(source_rev) <= int(target_rev_last):
        raise InternalError("keep-revnum mode is enabled, "
            "but source revision (r%s) is less-than-or-equal last target revision (r%s)" % \
            (source_rev, target_rev_last))
    if int(target_rev_last) < int(source_rev)-1:
        # Add "padding" target revisions to keep source and target rev #'s identical
        if os.path.exists(wc_target_tmp):
            shutil.rmtree(wc_target_tmp)
        run_svn(["checkout", "-r", "HEAD", "--depth=empty", target_repos_url, wc_target_tmp])
        for rev_num in range(int(target_rev_last)+1, int(source_rev)):
            run_svn(["propset", "svn2svn:keep-revnum", rev_num, wc_target_tmp])
            output = run_svn(["commit", "-m", "", wc_target_tmp])
            rev_num_tmp = parse_svn_commit_rev(output) if output else None
            assert rev_num == rev_num_tmp
            ui.status("Committed revision %s (keep-revnum).", rev_num)
            target_rev_last = rev_num
        shutil.rmtree(wc_target_tmp)
        # Update our target working-copy, to ensure everything says it's at the new HEAD revision
        run_svn(["update"])
    return target_rev_last

def disp_svn_log_summary(log_entry):
    ui.status("------------------------------------------------------------------------")
    ui.status("r%s | %s | %s",
        log_entry['revision'],
        log_entry['author'],
        str(datetime.fromtimestamp(int(log_entry['date'])).isoformat(' ')))
    ui.status(log_entry['message'])

def real_main(args, parser):
    global source_url, target_url, rev_map
    source_url = args.pop(0).rstrip("/")    # e.g. 'http://server/svn/source/trunk'
    target_url = args.pop(0).rstrip("/")    # e.g. 'file:///svn/target/trunk'
    ui.status("options: %s", str(options), level=ui.DEBUG, color='GREEN')

    # Make sure that both the source and target URL's are valid
    source_info = svnclient.get_svn_info(source_url)
    assert is_child_path(source_url, source_info['repos_url'])
    target_info = svnclient.get_svn_info(target_url)
    assert is_child_path(target_url, target_info['repos_url'])

    # Init global vars
    global source_repos_url,source_base,source_repos_uuid
    source_repos_url = source_info['repos_url']       # e.g. 'http://server/svn/source'
    source_base = source_url[len(source_repos_url):]  # e.g. '/trunk'
    source_repos_uuid = source_info['repos_uuid']
    global target_repos_url
    target_repos_url = target_info['repos_url']

    # Init start and end revision
    try:
        source_start_rev = svnclient.get_svn_rev(source_repos_url, options.rev_start if options.rev_start else 1)
    except ExternalCommandFailed:
        parser.error("invalid start source revision value: %s" % (options.rev_start))
    try:
        source_end_rev   = svnclient.get_svn_rev(source_repos_url, options.rev_end   if options.rev_end   else "HEAD")
    except ExternalCommandFailed:
        parser.error("invalid end source revision value: %s" % (options.rev_end))
    ui.status("Using source revision range %s:%s", source_start_rev, source_end_rev, level=ui.VERBOSE)

    # TODO: If options.keep_date, should we try doing a "svn propset" on an *existing* revision
    #       as a sanity check, so we check if the pre-revprop-change hook script is correctly setup
    #       before doing first replay-commit?

    target_rev_last =  target_info['revision']   # Last revision # in the target repo
    wc_target = os.path.abspath('_wc_target')
    wc_target_tmp = os.path.abspath('_tmp_wc_target')
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
        ui.status("Checking-out _wc_target...", level=ui.VERBOSE)
        svnclient.svn_checkout(target_url, wc_target)
    os.chdir(wc_target)

    if not options.cont_from_break:
        # TODO: Warn user if trying to start (non-continue) into a non-empty target path?
        # Get the first log entry at/after source_start_rev, which is where
        # we'll do the initial import from.
        it_log_start = svnclient.iter_svn_log_entries(source_url, source_start_rev, source_end_rev, get_changed_paths=False)
        for source_start_log in it_log_start:
            break
        if not source_start_log:
            raise InternalError("Unable to find any matching revisions between %s:%s in source_url: %s" % \
                (source_start_rev, source_end_rev, source_url))

        # This is the revision we will start from for source_url
        source_start_rev = source_rev = int(source_start_log['revision'])
        ui.status("Starting at source revision %s.", source_start_rev, level=ui.VERBOSE)
        ui.status("")
        if options.keep_revnum and source_rev > target_rev_last:
            target_rev_last = keep_revnum(source_rev, target_rev_last, wc_target_tmp)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        disp_svn_log_summary(svnclient.get_one_svn_log_entry(source_url, source_rev, source_rev))
        # Export and add file-contents from source_url@source_start_rev
        top_paths = run_svn(["list", "-r", source_rev, source_url+"@"+str(source_rev)])
        top_paths = top_paths.strip("\n").split("\n")
        for path in top_paths:
            # For each top-level file/folder...
            if not path:
                continue
            # Directories have a trailing slash in the "svn list" output
            path_is_dir = True if path[-1] == "/" else False
            path_offset = path.rstrip('/') if path_is_dir else path
            if in_svn(path_offset, prefix="  "):
                raise InternalError("Cannot replay history on top of pre-existing structure: %s" % source_url+"/"+path_offset)
            if path_is_dir and not os.path.exists(path_offset):
                os.makedirs(path_offset)
            run_svn(["export", "--force", "-r" , source_rev, source_url+"/"+path_offset+"@"+str(source_rev), path_offset])
            run_svn(["add", path_offset])
        # Update any properties on the newly added content
        paths = run_svn(["list", "--recursive", "-r", source_rev, source_url+"@"+str(source_rev)])
        paths = paths.strip("\n").split("\n")
        if options.keep_prop:
            sync_svn_props(source_url, source_rev, "")
        for path in paths:
            if not path:
                continue
            # Directories have a trailing slash in the "svn list" output
            path_is_dir = True if path[-1] == "/" else False
            path_offset = path.rstrip('/') if path_is_dir else path
            ui.status(" A %s", source_base+"/"+path_offset, level=ui.VERBOSE)
            if options.keep_prop:
                sync_svn_props(source_url, source_rev, path_offset)
        # Commit the initial import
        num_entries_proc += 1
        target_revprops = gen_tracking_revprops(source_rev)   # Build source-tracking revprop's
        target_rev = commit_from_svn_log_entry(source_start_log, target_revprops=target_revprops)
        if target_rev:
            # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
            set_rev_map(source_rev, target_rev)
            # Update our target working-copy, to ensure everything says it's at the new HEAD revision
            run_svn(["update"])
            commit_count += 1
            target_rev_last = target_rev
    else:
        # Re-build the rev_map based on any already-replayed history in target_url
        build_rev_map(target_url, target_rev_last, source_info)
        if not rev_map:
            parser.error("called with continue-mode, but no already-replayed source history found in target_url")
        source_start_rev = int(max(rev_map, key=rev_map.get))
        assert source_start_rev
        ui.status("Continuing from source revision %s.", source_start_rev, level=ui.VERBOSE)
        ui.status("")

    if options.keep_revnum and source_start_rev < target_rev_last:
        parser.error("last target revision is equal-or-higher than starting source revision; "
                     "cannot use --keep-revnum mode")

    svn_vers_t = svnclient.get_svn_client_version()
    svn_vers = float(".".join(map(str, svn_vers_t[0:2])))

    # Load SVN log starting from source_start_rev + 1
    it_log_entries = svnclient.iter_svn_log_entries(source_url, source_start_rev+1, source_end_rev, get_revprops=True) if source_start_rev < source_end_rev else []
    source_rev = None

    # TODO: Now that commit_from_svn_log_entry() might try to do a "svn propset svn:date",
    #       we might want some better KeyboardInterupt handilng here, to ensure that
    #       commit_from_svn_log_entry() always runs as an atomic unit.
    try:
        for log_entry in it_log_entries:
            if options.entries_proc_limit:
                if num_entries_proc >= options.entries_proc_limit:
                    break
            # Replay this revision from source_url into target_url
            source_rev = log_entry['revision']
            if options.keep_revnum:
                target_rev_last = keep_revnum(source_rev, target_rev_last, wc_target_tmp)
            disp_svn_log_summary(log_entry)
            # Process all the changed-paths in this log entry
            commit_paths = []
            process_svn_log_entry(log_entry, commit_paths)
            num_entries_proc += 1
            # Commit any changes made to _wc_target
            target_revprops = gen_tracking_revprops(source_rev)   # Build source-tracking revprop's
            target_rev = commit_from_svn_log_entry(log_entry, commit_paths, target_revprops=target_revprops)
            if target_rev:
                # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
                source_rev = log_entry['revision']
                set_rev_map(source_rev, target_rev)
                target_rev_last = target_rev
                # Update our target working-copy, to ensure everything says it's at the new HEAD revision
                run_svn(["update"])
                commit_count += 1
                # Run "svn cleanup" every 100 commits if SVN 1.7+, to clean-up orphaned ".svn/pristines/*"
                if svn_vers >= 1.7 and (commit_count % 100 == 0):
                    run_svn(["cleanup"])
        if not source_rev:
            # If there were no new source_url revisions to process, init source_rev
            # for the "finally" message below to be the last source revision replayed.
            source_rev = source_start_rev

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
        print "\nFinished at source revision %s%s." % (source_rev, " (dry-run)" if options.dry_run else "")

def main():
    # Defined as entry point. Must be callable without arguments.
    usage = "svn2svn, version %s\n" % str(full_version) + \
            "<http://nynim.org/projects/svn2svn> <https://github.com/tonyduckles/svn2svn>\n\n" + \
            "Usage: %prog [OPTIONS] source_url target_url\n"
    description = """\
Replicate (replay) history from one SVN repository to another. Maintain
logical ancestry wherever possible, so that 'svn log' on the replayed repo
will correctly follow file/folder renames.

Examples:
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
    parser.add_option("-v", "--verbose", dest="verbosity", action="count", default=1,
                      help="enable additional output (use -vv or -vvv for more)")
    parser.add_option("-a", "--archive", action="store_true", dest="archive", default=False,
                      help="archive/mirror mode; same as -UDP (see REQUIRE's below)\n"
                           "maintain same commit author, same commit time, and file/dir properties")
    parser.add_option("-U", "--keep-author", action="store_true", dest="keep_author", default=False,
                      help="maintain same commit authors (svn:author) as source\n"
                           "(REQUIRES target_url be non-auth'd, e.g. file://-based, since this uses --username to set author)")
    parser.add_option("-D", "--keep-date", action="store_true", dest="keep_date", default=False,
                      help="maintain same commit time (svn:date) as source\n"
                           "(REQUIRES 'pre-revprop-change' hook script to allow 'svn:date' changes)")
    parser.add_option("-P", "--keep-prop", action="store_true", dest="keep_prop", default=False,
                      help="maintain same file/dir SVN properties as source")
    parser.add_option("-R", "--keep-revnum", action="store_true", dest="keep_revnum", default=False,
                      help="maintain same rev #'s as source. creates placeholder target "
                            "revisions (by modifying a 'svn2svn:keep-revnum' property at the root of the target repo)")
    parser.add_option("-c", "--continue", action="store_true", dest="cont_from_break",
                      help="continue from last source commit to target (based on svn2svn:* revprops)")
    parser.add_option("-r", "--revision", type="string", dest="revision", metavar="ARG",
                      help="revision range to replay from source_url\n"
                           "A revision argument can be one of:\n"
                           "   START        start rev # (end will be 'HEAD')\n"
                           "   START:END    start and ending rev #'s\n"
                           "Any revision # formats which SVN understands are "
                           "supported, e.g. 'HEAD', '{2010-01-31}', etc.")
    parser.add_option("-u", "--log-author", action="store_true", dest="log_author", default=False,
                      help="append source commit author to replayed commit mesages")
    parser.add_option("-d", "--log-date", action="store_true", dest="log_date", default=False,
                      help="append source commit time to replayed commit messages")
    parser.add_option("-l", "--limit", type="int", dest="entries_proc_limit", metavar="NUM",
                      help="maximum number of source revisions to process")
    parser.add_option("-n", "--dry-run", action="store_true", dest="dry_run", default=False,
                      help="process next source revision but don't commit changes to "
                           "target working-copy (forces --limit=1)")
    parser.add_option("--debug", dest="verbosity", const=ui.DEBUG, action="store_const",
                      help="enable debugging output (same as -vvv)")
    global options
    options, args = parser.parse_args()
    if len(args) != 2:
        parser.error("incorrect number of arguments")
    if options.verbosity < 10:
        # Expand multiple "-v" arguments to a real ui._level value
        options.verbosity *= 10
    if options.dry_run:
        # When in dry-run mode, only try to process the next log_entry
        options.entries_proc_limit = 1
    options.rev_start = None
    options.rev_end   = None
    if options.revision:
        # Reg-ex for matching a revision arg (http://svnbook.red-bean.com/en/1.5/svn.tour.revs.specifiers.html#svn.tour.revs.dates)
        rev_patt = '[0-9A-Z]+|\{[0-9A-Za-z/\\ :-]+\}'
        rev = None
        match = re.match('^('+rev_patt+'):('+rev_patt+')$', options.revision)  # First try start:end match
        if match is None: match = re.match('^('+rev_patt+')$', options.revision)   # Next, try start match
        if match is None:
            parser.error("unexpected --revision argument format; see 'svn help log' for valid revision formats")
        rev = match.groups()
        options.rev_start = rev[0] if len(rev)>0 else None
        options.rev_end   = rev[1] if len(rev)>1 else None
    if options.archive:
        options.keep_author = True
        options.keep_date   = True
        options.keep_prop   = True
    ui.update_config(options)
    return real_main(args, parser)


if __name__ == "__main__":
    sys.exit(main() or 0)
