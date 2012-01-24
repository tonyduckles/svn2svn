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
from ..errors import (ExternalCommandFailed, UnsupportedSVNAction)

import sys
import os
import time
import traceback
from optparse import OptionParser,OptionGroup
from datetime import datetime
from operator import itemgetter

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

def in_svn(p, in_repo=False):
    """
    Check if a given file/folder is being tracked by Subversion.
    Prior to SVN 1.6, we could "cheat" and look for the existence of ".svn" directories.
    With SVN 1.7 and beyond, WC-NG means only a single top-level ".svn" at the root of the working-copy.
    Use "svn status" to check the status of the file/folder.
    """
    entries = svnclient.get_svn_status(p)
    if not entries:
      return False
    d = entries[0]
    # If caller requires this path to be in the SVN repo, prevent returning True for locally-added paths.
    if in_repo and (d['status'] == 'added' or d['revision'] is None):
        return False
    return True if (d['type'] == 'normal' or d['status'] == 'added') else False

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
    if debug:
        print prefix+"\x1b[33m" + ">> find_svn_ancestors: Start: ("+svn_repos_url+") source_path: "+source_path+"@"+str(source_rev)+"  base_path: "+base_path + "\x1b[0m"
    done = False
    working_path = base_path+"/"+source_path
    working_rev  = source_rev
    first_iter_done = False
    ancestors_temp = []
    while not done:
        # Get the first "svn log" entry for this path (relative to @rev)
        if debug:
            print prefix+"\x1b[33m" + ">> find_svn_ancestors: " + svn_repos_url + working_path+"@"+str(working_rev) + "\x1b[0m"
        log_entry = svnclient.get_first_svn_log_entry(svn_repos_url + working_path+"@"+str(working_rev), 1, str(working_rev), True)
        if not log_entry:
            if debug:
                print prefix+"\x1b[33m" + ">> find_svn_ancestors: Done: no log_entry" + "\x1b[0m"
            done = True
            break
        # If we found a copy-from case which matches our base_path, we're done.
        # ...but only if we've at least tried to search for the first copy-from path.
        if first_iter_done and working_path.startswith(base_path):
            if debug:
                print prefix+"\x1b[33m" + ">> find_svn_ancestors: Done: Found working_path.startswith(base_path) and first_iter_done=True" + "\x1b[0m"
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
            if debug:
                print prefix+"\x1b[33m" + ">> find_svn_ancestors: Done: No matching changed_paths" + "\x1b[0m"
            done = True
            continue
        # Reverse-sort any matches, so that we start with the most-granular (deepest in the tree) path.
        changed_paths = sorted(changed_paths_temp, key=itemgetter('path'), reverse=True)
        # Find the action for our working_path in this revision. Use a loop to check in reverse order,
        # so that if the target file/folder is "M" but has a parent folder with an "A" copy-from.
        for v in changed_paths:
            d = v['data']
            path = d['path']
            # Check action-type for this file
            action = d['action']
            if action not in 'MARD':
                raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                    % (log_entry['revision'], action))
            if debug:
                debug_desc = "> " + action + " " + path
                if d['copyfrom_path']:
                    debug_desc += " (from " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + ")"
                print prefix+"\x1b[33m" + debug_desc + "\x1b[0m"
            if action == 'D':
                # If file/folder was deleted, it has no ancestor
                ancestors_temp = []
                if debug:
                    print prefix+"\x1b[33m" + ">> find_svn_ancestors: Done: deleted" + "\x1b[0m"
                done = True
                break
            if action in 'RA':
                # If file/folder was added/replaced but not a copy, it has no ancestor
                if not d['copyfrom_path']:
                    ancestors_temp = []
                    if debug:
                        print prefix+"\x1b[33m" + ">> find_svn_ancestors: Done: "+("Added" if action == "A" else "Replaced")+" with no copyfrom_path" + "\x1b[0m"
                    done = True
                    break
                # Else, file/folder was added/replaced and is a copy, so add an entry to our ancestors list
                # and keep checking for ancestors
                if debug:
                    print prefix+"\x1b[33m" + ">> find_svn_ancestors: Found copy-from ("+action+"): " + \
                          path + " --> " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + "\x1b[0m"
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
    else:
        if debug:
            print prefix+"\x1b[33m" + ">> find_svn_ancestors: No ancestor-chain found: " + svn_repos_url+base_path+"/"+source_path+"@"+(str(source_rev)) + "\x1b[0m"
    return ancestors

def get_rev_map(rev_map, src_rev, prefix):
    """
    Find the equivalent rev # in the target repo for the given rev # from the source repo.
    """
    if debug:
        print prefix + "\x1b[32m" + ">> get_rev_map("+str(src_rev)+")" + "\x1b[0m"
    # Find the highest entry less-than-or-equal-to src_rev
    for rev in range(src_rev, 0, -1):
        if debug:
            print prefix + "\x1b[32m" + ">> get_rev_map: rev="+str(rev)+"  in_rev_map="+str(rev in rev_map) + "\x1b[0m"
        if rev in rev_map:
            return rev_map[rev]
    # Else, we fell off the bottom of the rev_map. Ruh-roh...
    return None

def get_svn_dirlist(svn_path, svn_rev = ""):
    """
    Get a list of all the child contents (recusive) of the given folder path.
    """
    args = ["list"]
    path = svn_path
    if svn_rev:
        args += ["-r", str(svn_rev)]
        path += "@"+str(svn_rev)
    args += [path]
    paths = run_svn(args, False, True)
    paths = paths.strip("\n").split("\n") if len(paths)>1 else []
    return paths

def _add_export_path(export_paths, path_offset):
    found = False
    for p in export_paths:
        if path_offset.startswith(p):
            found = True
            break
    if not found:
        export_paths.append(path_offset)
    return export_paths

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
    if debug:
        print prefix + "\x1b[32m" + ">> do_svn_add: " + source_base+"/"+path_offset+"@"+str(source_rev) + \
              ("  (parent-copyfrom: "+parent_copyfrom_path+"@"+str(parent_copyfrom_rev)+")" if parent_copyfrom_path else "") + "\x1b[0m"
    # Check if the given path has ancestors which chain back to the current source_base
    found_ancestor = False
    ancestors = find_svn_ancestors(source_repos_url, source_base, path_offset, source_rev, prefix+"  ")
    # ancestors[n] is the original (pre-branch-copy) trunk path.
    # ancestors[n-1] is the first commit on the new branch.
    copyfrom_path = ancestors[len(ancestors)-1]['path']     if ancestors else ""
    copyfrom_rev  = ancestors[len(ancestors)-1]['revision'] if ancestors else ""
    if ancestors:
        # The copy-from path has ancestory back to source_url.
        if debug:
            print prefix + "\x1b[32;1m" + ">> do_svn_add: Check copy-from: Found parent: " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
        found_ancestor = True
        # Map the copyfrom_rev (source repo) to the equivalent target repo rev #. This can
        # return None in the case where copyfrom_rev is *before* our source_start_rev.
        tgt_rev = get_rev_map(rev_map, copyfrom_rev, prefix+"  ")
        if debug:
            print prefix + "\x1b[32m" + ">> do_svn_add: get_rev_map: " + str(copyfrom_rev) + " (source) -> " + str(tgt_rev) + " (target)" + "\x1b[0m"
    else:
        if debug:
            print prefix + "\x1b[32;1m" + ">> do_svn_add: Check copy-from: No ancestor chain found." + "\x1b[0m"
        found_ancestor = False
    if found_ancestor and tgt_rev:
        # Check if this path_offset in the target WC already has this ancestry, in which
        # case there's no need to run the "svn copy" (again).
        path_in_svn = in_svn(path_offset)
        log_entry = svnclient.get_last_svn_log_entry(path_offset, 1, 'HEAD', get_changed_paths=False) if in_svn(path_offset, True) else []
        if (not log_entry or (log_entry['revision'] != tgt_rev)):
            copyfrom_offset = copyfrom_path[len(source_base):].strip('/')
            if debug:
                print prefix + "\x1b[32m" + ">> do_svn_add: svn_copy: Copy-from: " + copyfrom_path+"@"+str(copyfrom_rev) + "\x1b[0m"
                print prefix + "in_svn("+path_offset+") = " + str(path_in_svn)
                print prefix + "copyfrom_path: "+copyfrom_path+"  parent_copyfrom_path: "+parent_copyfrom_path
                print prefix + "copyfrom_rev: "+str(copyfrom_rev)+"  parent_copyfrom_rev: "+str(parent_copyfrom_rev)
            if path_in_svn and \
               ((parent_copyfrom_path and copyfrom_path.startswith(parent_copyfrom_path)) and \
                (parent_copyfrom_rev and copyfrom_rev == parent_copyfrom_rev)):
                # When being called recursively, if this child entry has the same ancestor as the
                # the parent, then no need to try to run another "svn copy".
                if debug:
                    print prefix + "\x1b[32m" + ">> do_svn_add: svn_copy: Same ancestry as parent: " + parent_copyfrom_path+"@"+str(parent_copyfrom_rev) + "\x1b[0m"
                pass
            else:
                # Copy this path from the equivalent path+rev in the target repo, to create the
                # equivalent history.
                if parent_copyfrom_path and svnlog_verbose:
                    # If we have a parent copy-from path, we mis-match that so display a status
                    # message describing the action we're mimic'ing. If path_in_svn, then this
                    # is logically a "replace" rather than an "add".
                    print " "+('R' if path_in_svn else 'A')+" "+source_base+"/"+path_offset+" (from "+ancestors[1]['path']+"@"+str(copyfrom_rev)+")"
                if path_in_svn:
                    # If local file is already under version-control, then this is a replace.
                    if debug:
                        print prefix + "\x1b[32m" + ">> do_svn_add: pre-copy: local path already exists: " + path_offset + "\x1b[0m"
                    run_svn(["remove", "--force", path_offset])
                run_svn(["copy", "-r", tgt_rev, target_url+"/"+copyfrom_offset+"@"+str(tgt_rev), path_offset])
                # Export the final version of this file/folder from the source repo, to make
                # sure we're up-to-date.
                export_paths = _add_export_path(export_paths, path_offset)
        else:
            print prefix + "\x1b[32m" + ">> do_svn_add: Skipped 'svn copy': " + path_offset + "\x1b[0m"
    else:
        # Else, either this copy-from path has no ancestry back to source_url OR copyfrom_rev comes
        # before our initial source_start_rev (i.e. tgt_rev == None), so can't do a "svn copy".
        # Create (parent) directory if needed.
        # TODO: This is (nearly) a duplicate of code in process_svn_log_entry(). Should this be
        #       split-out to a shared tag?
        p_path = path_offset if is_dir else os.path.dirname(path_offset).strip() or '.'
        if not os.path.exists(p_path):
            run_svn(["mkdir", p_path])
        if not in_svn(path_offset):
            if is_dir:
                # Export the final verison of all files in this folder.
                export_paths = _add_export_path(export_paths, path_offset)
            else:
                # Export the final verison of this file. We *need* to do this before running
                # the "svn add", even if we end-up re-exporting this file again via export_paths.
                run_svn(["export", "--force", "-r", str(source_rev),
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
    if debug:
        print prefix + "\x1b[32m" + ">> do_svn_add_dir: paths_local:  " + str(paths_local) + "\x1b[0m"
        print prefix + "\x1b[32m" + ">> do_svn_add_dir: paths_remote: " + str(paths_remote) + "\x1b[0m"
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
            if svnlog_verbose:
                print " D " + source_base+"/"+path_offset+"/"+path
            run_svn(["remove", "--force", path_offset+"/"+path])
            # TODO: Does this handle deleted folders too? Wouldn't want to have a case
            #       where we only delete all files from folder but leave orphaned folder around.

def process_svn_log_entry(log_entry, source_repos_url, source_url, target_url, \
                          rev_map, commit_paths = [], prefix = ""):
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
    removed_paths = []
    export_paths = []
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
            raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                % (source_rev, action))
        if svnlog_verbose and (action not in 'D'):
            # (Note: Skip displaying action message for 'D' here since we'll display that
            #  message when we process the deferred delete actions at the end.)
            msg = " " + action + " " + d['path']
            if d['copyfrom_path']:
                msg += " (from " + d['copyfrom_path']+"@"+str(d['copyfrom_revision']) + ")"
            print prefix + msg

        # Try to be efficient and keep track of an explicit list of paths in the
        # working copy that changed. If we commit from the root of the working copy,
        # then SVN needs to crawl the entire working copy looking for pending changes.
        # But, if we gather too many paths to commit, then we wipe commit_paths below
        # and end-up doing a commit at the root of the working-copy.
        if len (commit_paths) < 100:
            commit_paths.append(path_offset)

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
            # If we have any queued deletions for this same path, remove those if we're re-adding this path.
            if path_offset in removed_paths:
                removed_paths.remove(path_offset)
            # Determine where to export from.
            svn_copy = False
            path_is_dir = True if d['kind'] == 'dir' else False
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
                    export_paths = _add_export_path(export_paths, path_offset)
                else:
                    # Export the final verison of this file. We *need* to do this before running
                    # the "svn add", even if we end-up re-exporting this file again via export_paths.
                    run_svn(["export", "--force", "-r", str(source_rev),
                             source_repos_url+source_base+"/"+path_offset+"@"+str(source_rev), path_offset])
                # TODO: Do we need the in_svn check here?
                #if not in_svn(path_offset):
                run_svn(["add", "--parents", path_offset])
                # TODO: Need to copy SVN properties from source repos

        elif action == 'D':
            # Queue "svn remove" commands, to allow the action == 'A' handling the opportunity
            # to do smart "svn copy" handling on copy/move/renames.
            if not path_offset in removed_paths:
                removed_paths.append(path_offset)

        elif action == 'M':
            # TODO: Is "svn merge -c" correct here? Should this just be an "svn export" plus
            #       proplist updating?
            out = run_svn(["merge", "-c", str(source_rev), "--non-recursive",
                     "--non-interactive", "--accept=theirs-full",
                     source_url+"/"+path_offset+"@"+str(source_rev), path_offset])

        else:
            raise SVNError("Internal Error: process_svn_log_entry: Unhandled 'action' value: '%s'"
                % action)

    # Process any deferred removed actions
    if removed_paths:
        path_base = source_url[len(source_repos_url):]
        for path_offset in removed_paths:
            if svnlog_verbose:
                print " D " + path_base+"/"+path_offset
            run_svn(["remove", "--force", path_offset])
    # Export the final version of all add'd paths from source_url
    if export_paths:
        for path_offset in export_paths:
            run_svn(["export", "--force", "-r", str(source_rev),
                     source_repos_url+source_base+"/"+path_offset+"@"+str(source_rev), path_offset])

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
    commit_paths = []
    process_svn_log_entry(log_entry, source_repos_url, source_url, target_url,
                          rev_map, commit_paths)
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

def run_parser(parser):
    """
    Add common options to an OptionParser instance, and run parsing.
    """
    parser.add_option("", "--version", dest="show_version", action="store_true",
        help="show version and exit")
    parser.remove_option("--help")
    parser.add_option("-h", "--help", dest="show_help", action="store_true",
        help="show this help message and exit")
    parser.add_option("-v", "--verbose", dest="verbosity", const=20,
                      default=10, action="store_const",
                      help="enable additional output")
    parser.add_option("--debug", dest="verbosity", const=30,
                      action="store_const",
                      help="enable debugging output")
    options, args = parser.parse_args()
    if options.show_help:
        parser.print_help()
        sys.exit(0)
    if options.show_version:
        prog_name = os.path.basename(sys.argv[0])
        print prog_name, full_version
        sys.exit(0)
    ui.update_config(options)
    return options, args

def display_parser_error(parser, message):
    """
    Display an options error, and terminate.
    """
    print "error:", message
    print
    parser.print_help()
    sys.exit(1)

def real_main(options, args):
    source_url = args.pop(0).rstrip("/")
    target_url = args.pop(0).rstrip("/")
    if options.keep_author:
        keep_author = True
    else:
        keep_author = False

    # Find the greatest_rev in the source repo
    svn_info = svnclient.get_svn_info(source_url)
    greatest_rev = svn_info['revision']
    # Get the base URL for the source repos, e.g. 'svn://svn.example.com/svn/repo'
    source_repos_url = svn_info['repos_url']
    # Get the UUID for the source repos
    source_repos_uuid = svn_info['repos_uuid']

    wc_target = "_wc_target"
    rev_map = {}

    # if old working copy does not exist, disable continue mode
    # TODO: Better continue support. Maybe include source repo's rev # in target commit info?
    if not os.path.exists(wc_target):
        options.cont_from_break = False

    if not options.cont_from_break:
        # Get log entry for the SVN revision we will check out
        if options.svn_rev:
            # If specify a rev, get log entry just before or at rev
            svn_start_log = svnclient.get_last_svn_log_entry(source_url, 1, options.svn_rev, False)
        else:
            # Otherwise, get log entry of branch creation
            # TODO: This call is *very* expensive on a repo with lots of revisions.
            #       Even though the call is passing --limit 1, it seems like that limit-filter
            #       is happening after SVN has fetched the full log history.
            svn_start_log = svnclient.get_first_svn_log_entry(source_url, 1, greatest_rev, False)

        # This is the revision we will start from for source_url
        source_start_rev = svn_start_log['revision']

        # Check out a working copy of target_url
        wc_target = os.path.abspath(wc_target)
        if os.path.exists(wc_target):
            shutil.rmtree(wc_target)
        svnclient.svn_checkout(target_url, wc_target)
        os.chdir(wc_target)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        paths = run_svn(["list", "-r", str(source_start_rev), source_url+"@"+str(source_start_rev)])
        if len(paths)>1:
            disp_svn_log_summary(svnclient.get_one_svn_log_entry(source_url, source_start_rev, source_start_rev))
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
                run_svn(["export", "--force", "-r" , str(source_start_rev), source_url+"/"+path+"@"+str(source_start_rev), path])
                run_svn(["add", path])
            revprops = [{'name':'source_uuid', 'value':source_repos_uuid},
                        {'name':'source_url',  'value':source_url},
                        {'name':'source_rev',  'value':source_start_rev}]
            commit_from_svn_log_entry(svn_start_log, [], keep_author=keep_author, revprops=revprops)
            print "(Finished source rev #"+str(source_start_rev)+")"
    else:
        wc_target = os.path.abspath(wc_target)
        os.chdir(wc_target)
        # TODO: Need better resume support. For the time being, expect caller explictly passes in resume revision.
        source_start_rev = options.svn_rev
        if source_start_rev < 1:
            display_error("Invalid arguments\n\nNeed to pass result rev # (-r) when using continue-mode (-c)", False)

    # Load SVN log starting from source_start_rev + 1
    it_log_entries = svnclient.iter_svn_log_entries(source_url, source_start_rev + 1, greatest_rev)

    try:
        for log_entry in it_log_entries:
            # Replay this revision from source_url into target_url
            pull_svn_rev(log_entry, source_repos_url, source_repos_uuid, source_url,
                         target_url, rev_map, keep_author)
            # Update our target working-copy, to ensure everything says it's at the new HEAD revision
            run_svn(["up"])
            # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
            dup_info = get_svn_info(target_url)
            dup_rev = dup_info['revision']
            source_rev = log_entry['revision']
            if debug:
                print "\x1b[32m" + ">> main: rev_map.add: source_rev=%s target_rev=%s" % (source_rev, dup_rev) + "\x1b[0m"
            rev_map[source_rev] = dup_rev

    except KeyboardInterrupt:
        print "\nStopped by user."
        run_svn(["cleanup"])
        run_svn(["revert", "--recursive", "."])
        # TODO: Run "svn status" and pro-actively delete any "?" orphaned entries, to clean-up the WC?
    except:
        print "\nCommand failed with following error:\n"
        traceback.print_exc()
        run_svn(["cleanup"])
        run_svn(["revert", "--recursive", "."])
        # TODO: Run "svn status" and pro-actively delete any "?" orphaned entries, to clean-up the WC?
    finally:
        run_svn(["up"])
        print "\nFinished!"

def main():
    # Defined as entry point. Must be callable without arguments.
    usage = "Usage: %prog [OPTIONS] source_url target_url"
    parser = OptionParser(usage)
    parser.add_option("-r", "--revision", type="int", dest="svn_rev", metavar="REV",
                      help="initial SVN revision to checkout from")
    parser.add_option("-a", "--keep-author", action="store_true", dest="keep_author",
                      help="maintain original Author info from source repo")
    parser.add_option("-c", "--continue", action="store_true", dest="cont_from_break",
                      help="continue from previous break")
    (options, args) = run_parser(parser)
    if len(args) != 2:
        display_parser_error(parser, "incorrect number of arguments")
    return real_main(options, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
