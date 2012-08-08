from svn2svn import ui
from svn2svn import svnclient

import operator


def in_svn(p, require_in_repo=False, prefix=""):
    """
    Check if a given file/folder is being tracked by Subversion.
    Prior to SVN 1.6, we could "cheat" and look for the existence of ".svn" directories.
    With SVN 1.7 and beyond, WC-NG means only a single top-level ".svn" at the root of the working-copy.
    Use "svn status" to check the status of the file/folder.
    """
    entries = svnclient.status(p, non_recursive=True)
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

def join_path(base, child):
    base.rstrip('/')
    return base+"/"+child if child else base

def find_svn_ancestors(svn_repos_url, start_path, start_rev, stop_base_path=None, prefix=""):
    """
    Given an initial starting path+rev, walk the SVN history backwards to inspect the
    ancestry of that path, optionally seeing if it traces back to stop_base_path.

    Build an array of copyfrom_path and copyfrom_revision pairs for each of the "svn copy"'s.
    If we find a copyfrom_path which stop_base_path is a substring match of (e.g. we crawled
    back to the initial branch-copy from trunk), then return the collection of ancestor
    paths.  Otherwise, copyfrom_path has no ancestry compared to stop_base_path.

    This is useful when comparing "trunk" vs. "branch" paths, to handle cases where a
    file/folder was renamed in a branch and then that branch was merged back to trunk.

    'svn_repos_url' is the full URL to the root of the SVN repository,
      e.g. 'file:///path/to/repo'
    'start_path' is the path in the SVN repo to the source path to start checking
      ancestry at, e.g. '/branches/fix1/projectA/file1.txt'.
    'start_rev' is the revision to start walking the history of start_path backwards from.
    'stop_base_path' is the path in the SVN repo to stop tracing ancestry once we've reached,
      i.e. the target path we're trying to trace ancestry back to, e.g. '/trunk'.
    """
    ui.status(prefix + ">> find_svn_ancestors: Start: (%s) start_path: %s  stop_base_path: %s",
        svn_repos_url, start_path+"@"+str(start_rev), stop_base_path, level=ui.DEBUG, color='YELLOW')
    done = False
    no_ancestry = False
    cur_path = start_path
    cur_rev  = start_rev
    first_iter_done = False
    ancestors = []
    while not done:
        # Get the first "svn log" entry for cur_path (relative to @cur_rev)
        ui.status(prefix + ">> find_svn_ancestors: %s", svn_repos_url+cur_path+"@"+str(cur_rev), level=ui.DEBUG, color='YELLOW')
        log_entry = svnclient.get_first_svn_log_entry(svn_repos_url+cur_path, 1, cur_rev)
        if not log_entry:
            ui.status(prefix + ">> find_svn_ancestors: Done: no log_entry", level=ui.DEBUG, color='YELLOW')
            done = True
            break
        # If we found a copy-from case which matches our stop_base_path, we're done.
        # ...but only if we've at least tried to search for the first copy-from path.
        if stop_base_path is not None and first_iter_done and is_child_path(cur_path, stop_base_path):
            ui.status(prefix + ">> find_svn_ancestors: Done: Found is_child_path(cur_path, stop_base_path) and first_iter_done=True", level=ui.DEBUG, color='YELLOW')
            done = True
            break
        first_iter_done = True
        # Search for any actions on our target path (or parent paths).
        changed_paths_temp = []
        for d in log_entry['changed_paths']:
            path = d['path']
            if is_child_path(cur_path, path):
                changed_paths_temp.append({'path': path, 'data': d})
        if not changed_paths_temp:
            # If no matches, then we've hit the end of the ancestry-chain.
            ui.status(prefix + ">> find_svn_ancestors: Done: No matching changed_paths", level=ui.DEBUG, color='YELLOW')
            done = True
            continue
        # Reverse-sort any matches, so that we start with the most-granular (deepest in the tree) path.
        changed_paths = sorted(changed_paths_temp, key=operator.itemgetter('path'), reverse=True)
        # Find the action for our cur_path in this revision. Use a loop to check in reverse order,
        # so that if the target file/folder is "M" but has a parent folder with an "A" copy-from
        # then we still correctly match the deepest copy-from.
        for v in changed_paths:
            d = v['data']
            path = d['path']
            # Check action-type for this file
            action = d['action']
            if action not in svnclient.valid_svn_actions:
                raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                    % (log_entry['revision'], action))
            ui.status(prefix + "> %s %s%s", action, path,
                (" (from %s)" % (d['copyfrom_path']+"@"+str(d['copyfrom_revision']))) if d['copyfrom_path'] else "",
                level=ui.DEBUG, color='YELLOW')
            if action == 'D':
                # If file/folder was deleted, ancestry-chain stops here
                if stop_base_path:
                    no_ancestry = True
                ui.status(prefix + ">> find_svn_ancestors: Done: deleted", level=ui.DEBUG, color='YELLOW')
                done = True
                break
            if action in 'RA':
                # If file/folder was added/replaced but not a copy, ancestry-chain stops here
                if not d['copyfrom_path']:
                    if stop_base_path:
                        no_ancestry = True
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
                ancestors.append({'path': cur_path, 'revision': log_entry['revision'],
                    'copyfrom_path': cur_path.replace(d['path'], d['copyfrom_path']), 'copyfrom_rev': d['copyfrom_revision']})
                cur_path = cur_path.replace(d['path'], d['copyfrom_path'])
                cur_rev =  d['copyfrom_revision']
                # Follow the copy and keep on searching
                break
    if stop_base_path and no_ancestry:
        # If we're tracing back ancestry to a specific target stop_base_path and
        # the ancestry-chain stopped before we reached stop_base_path, then return
        # nothing since there is no ancestry chaining back to that target.
        ancestors = []
    if ancestors:
        if ui.get_level() >= ui.DEBUG:
            max_len = 0
            for idx in range(len(ancestors)):
                d = ancestors[idx]
                max_len = max(max_len, len(d['path']+"@"+str(d['revision'])))
            ui.status(prefix + ">> find_svn_ancestors: Found parent ancestors:", level=ui.DEBUG, color='YELLOW_B')
            for idx in range(len(ancestors)):
                d = ancestors[idx]
                ui.status(prefix + " [%s] %s --> %s", idx,
                    str(d['path']+"@"+str(d['revision'])).ljust(max_len),
                    str(d['copyfrom_path']+"@"+str(d['copyfrom_rev'])),
                    level=ui.DEBUG, color='YELLOW')
    else:
        ui.status(prefix + ">> find_svn_ancestors: No ancestor-chain found: %s",
            svn_repos_url+start_path+"@"+str(start_rev), level=ui.DEBUG, color='YELLOW')
    return ancestors
