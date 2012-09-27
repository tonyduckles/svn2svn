"""
Replicate (replay) changesets from one SVN repository to another.
"""

from svn2svn import base_version, full_version
from svn2svn import ui
from svn2svn import shell
from svn2svn import svnclient
from svn2svn.shell import run_svn,run_shell_command
from svn2svn.errors import ExternalCommandFailed, UnsupportedSVNAction, InternalError, VerificationError
from svn2svn.run.common import in_svn, is_child_path, join_path, find_svn_ancestors
from parse import HelpFormatter
from breakhandler import BreakHandler

import sys
import os
import traceback
import operator
import optparse
import re
import urllib
from datetime import datetime

# Module-level variables/parameters
source_url = ""          # URL to source path in source SVN repo, e.g. 'http://server/svn/source/trunk'
source_repos_url = ""    # URL to root of source SVN repo,        e.g. 'http://server/svn/source'
source_base = ""         # Relative path of source_url in source SVN repo, e.g. '/trunk'
source_repos_uuid = ""   # UUID of source SVN repo
target_url =""           # URL to target path in target SVN repo, e.g. 'file:///svn/repo_target/trunk'
target_repos_url = ""    # URL to root of target SVN repo,        e.g. 'http://server/svn/target'
target_base = ""         # Relative path of target_url in target SVN repo, e.g. '/trunk'
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
    if options.beforecommit:
        # Run optional external shell hook here, for doing pre-commit filtering
        # $1 = Path to working copy
        # $2 = Source revision #
        args = [os.getcwd(), log_entry['revision']]
        run_shell_command(options.beforecommit, args=args)
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
            for c_path in commit_paths:
                args += [svnclient.safe_path(c_path)]
    rev_num = None
    if not options.dry_run:
        # Use BreakHandler class to temporarily redirect SIGINT handler, so that
        # "svn commit" + post-commit rev-prop updating is a quasi-atomic unit.
        # If user presses Ctrl-C during this, wait until after this full action
        # has finished raising the KeyboardInterrupt exception.
        bh = BreakHandler()
        bh.enable()
        # Run the "svn commit" command, and screen-scrape the target_rev value (if any)
        output = run_svn(args)
        rev_num = parse_svn_commit_rev(output) if output else None
        if rev_num is not None:
            if options.keep_date:
                run_svn(["propset", "--revprop", "-r", rev_num, "svn:date", log_entry['date_raw']])
            if options.keep_author:
                run_svn(["propset", "--revprop", "-r", rev_num, "svn:author",  log_entry['author']])
            ui.status("Committed revision %s (source r%s).", rev_num, log_entry['revision'])
        bh.disable()
        # Check if the user tried to press Ctrl-C
        if bh.trapped:
            raise KeyboardInterrupt
    return rev_num

def verify_commit(source_rev, target_rev, log_entry=None):
    """
    Compare the ancestry/content/properties between source_url vs target_url
    for a given revision.
    """
    error_cnt = 0
    # Gather the offsets in the source repo to check
    check_paths = []
    remove_paths = []
    # TODO: Need to make this ancestry aware
    if options.verify == 1 and log_entry is not None:  # Changed only
        ui.status("Verifying source revision %s (only-changed)...", source_rev, level=ui.VERBOSE)
        for d in log_entry['changed_paths']:
            path = d['path']
            if not is_child_path(path, source_base):
                continue
            if d['kind'] == "":
                d['kind'] = svnclient.get_kind(source_repos_url, path, source_rev, d['action'], log_entry['changed_paths'])
            assert (d['kind'] == 'file') or (d['kind'] == 'dir')
            path_is_dir =  True if d['kind'] == 'dir'  else False
            path_is_file = True if d['kind'] == 'file' else False
            path_offset = path[len(source_base):].strip("/")
            if d['action'] == 'D':
                remove_paths.append(path_offset)
            elif not path_offset in check_paths:
                ui.status("verify_commit: path [mode=changed]: kind=%s: %s", d['kind'], path, level=ui.DEBUG, color='YELLOW')
                if path_is_file:
                    ui.status("  "+"verify_commit [mode=changed]: check_paths.append('%s')", path_offset, level=ui.DEBUG, color='GREEN')
                    check_paths.append(path_offset)
                if path_is_dir:
                    if not d['action'] in 'AR':
                        continue
                    child_paths = svnclient.list(source_url.rstrip("/")+"/"+path_offset, source_rev, recursive=True)
                    for p in child_paths:
                        child_path_is_dir = True if p['kind'] == 'dir' else False
                        child_path_offset = p['path']
                        if not child_path_is_dir:
                            # Only check files
                            working_path = (path_offset+"/" if path_offset else "") + child_path_offset
                            if not working_path in check_paths:
                                ui.status("    "+"verify_commit [mode=changed]: check_paths.append('%s'+'/'+'%s')", path_offset, child_path_offset, level=ui.DEBUG, color='GREEN')
                                check_paths.append(working_path)
    if options.verify == 2:  # All paths
        ui.status("Verifying source revision %s (all)...", source_rev, level=ui.VERBOSE)
        child_paths = svnclient.list(source_url, source_rev, recursive=True)
        for p in child_paths:
            child_path_is_dir = True if p['kind'] == 'dir' else False
            child_path_offset = p['path']
            if not child_path_is_dir:
                # Only check files
                ui.status("verify_commit [mode=all]: check_paths.append('%s')", child_path_offset, level=ui.DEBUG, color='GREEN')
                check_paths.append(child_path_offset)

    # If there were any paths deleted in the last revision (options.verify=1 mode),
    # check that they were correctly deleted.
    if remove_paths:
        count_total = len(remove_paths)
        count = 0
        for path_offset in remove_paths:
            count += 1
            if in_svn(path_offset):
                ui.status(" (%s/%s) Verify path: FAIL: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA, color='RED')
                ui.status("VerificationError: Path removed in source rev r%s, but still exists in target WC: %s", source_rev, path_offset, color='RED')
                error_cnt +=1
            else:
                ui.status(" (%s/%s) Verify remove: OK: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA)

    # Compare each of the check_path entries between source vs. target
    if check_paths:
        source_rev_first = int(min(rev_map, key=rev_map.get)) or 1  # The first source_rev we replayed into target
        ui.status("verify_commit: source_rev_first:%s", source_rev_first, level=ui.DEBUG, color='YELLOW')
        count_total = len(check_paths)
        count = 0
        for path_offset in check_paths:
            count += 1
            if count % 500 == 0:
                ui.status("...processed %s (%s of %s)..." % (count, count, count_total), level=ui.VERBOSE)
            ui.status("verify_commit: path_offset:%s", path_offset, level=ui.DEBUG, color='YELLOW')
            source_log_entries = svnclient.run_svn_log(source_url.rstrip("/")+"/"+path_offset, source_rev, 1, source_rev-source_rev_first+1)
            target_log_entries = svnclient.run_svn_log(target_url.rstrip("/")+"/"+path_offset, target_rev, 1, target_rev)
            # Build a list of commits in source_log_entries which matches our
            # target path_offset.
            working_path = source_base+"/"+path_offset
            source_revs = []
            for log_entry in source_log_entries:
                source_rev_tmp = log_entry['revision']
                if source_rev_tmp < source_rev_first:
                    # Only process source revisions which have been replayed into target
                    break
                #ui.status("  [verify_commit] source_rev_tmp:%s, working_path:%s\n%s", source_rev_tmp, working_path, pp.pformat(log_entry), level=ui.DEBUG, color='MAGENTA')
                changed_paths_temp = []
                for d in log_entry['changed_paths']:
                    path = d['path']
                    # Match working_path or any parents
                    if is_child_path(working_path, path):
                        ui.status("  verify_commit: changed_path: %s %s@%s (parent:%s)", d['action'], path, source_rev_tmp, working_path, level=ui.DEBUG, color='YELLOW')
                        changed_paths_temp.append({'path': path, 'data': d})
                assert changed_paths_temp
                # Reverse-sort any matches, so that we start with the most-granular (deepest in the tree) path.
                changed_paths = sorted(changed_paths_temp, key=operator.itemgetter('path'), reverse=True)
                # Find the action for our working_path in this revision. Use a loop to check in reverse order,
                # so that if the target file/folder is "M" but has a parent folder with an "A" copy-from.
                working_path_next = working_path
                match_d = {}
                for v in changed_paths:
                    d = v['data']
                    if not match_d:
                        match_d = d
                    path = d['path']
                    if d['action'] not in svnclient.valid_svn_actions:
                        raise UnsupportedSVNAction("In SVN rev. %d: action '%s' not supported. Please report a bug!"
                            % (log_entry['revision'], d['action']))
                    if d['action'] in 'AR' and d['copyfrom_revision']:
                        # If we found a copy-from action for a parent path, adjust our
                        # working_path to follow the rename/copy-from, just like find_svn_ancestors().
                        working_path_next = working_path.replace(d['path'], d['copyfrom_path'])
                        match_d = d
                        break
                if is_child_path(working_path, source_base):
                    # Only add source_rev's where the path changed in this revision was a child
                    # of source_base, so that we silently ignore any history that happened on
                    # non-source_base paths (e.g. ignore branch history if we're only replaying trunk).
                    is_diff = False
                    d = match_d
                    if d['action'] == 'M':
                        # For action="M", we need to throw out cases where the only change was to
                        # a property which we ignore, e.g. "svn:mergeinfo".
                        if d['kind'] == "":
                            d['kind'] = svnclient.get_kind(source_repos_url, working_path, log_entry['revision'], d['action'], log_entry['changed_paths'])
                        assert (d['kind'] == 'file') or (d['kind'] == 'dir')
                        if d['kind'] == 'file':
                            # Check for file-content changes
                            # TODO: This should be made ancestor-aware, since the file won't always be at the same path in rev-1
                            sum1 = run_shell_command("svn cat -r %s '%s' | md5sum" % (source_rev_tmp, source_repos_url+working_path+"@"+str(source_rev_tmp)))
                            sum2 = run_shell_command("svn cat -r %s '%s' | md5sum" % (source_rev_tmp-1, source_repos_url+working_path_next+"@"+str(source_rev_tmp-1)))
                            is_diff = True if sum1 <> sum2 else False
                        if not is_diff:
                            # Check for property changes
                            props1 = svnclient.propget_all(source_repos_url+working_path, source_rev_tmp)
                            props2 = svnclient.propget_all(source_repos_url+working_path_next, source_rev_tmp-1)
                            # Ignore changes to "svn:mergeinfo", since we don't copy that
                            if 'svn:mergeinfo' in props1: del props1['svn:mergeinfo']
                            if 'svn:mergeinfo' in props2: del props2['svn:mergeinfo']
                            for prop in props1:
                                if prop not in props2 or \
                                        props1[prop] != props2[prop]:
                                    is_diff = True
                                    break
                            for prop in props2:
                                if prop not in props1 or \
                                        props1[prop] != props2[prop]:
                                    is_diff = True
                                    break
                        if not is_diff:
                            ui.status("  verify_commit: skip %s@%s", working_path, source_rev_tmp, level=ui.DEBUG, color='GREEN_B', bold=True)
                    else:
                        is_diff = True
                    if is_diff:
                        ui.status("  verify_commit: source_revs.append(%s), working_path:%s", source_rev_tmp, working_path, level=ui.DEBUG, color='GREEN_B')
                        source_revs.append({'path': working_path, 'revision': source_rev_tmp})
                working_path = working_path_next
            # Build a list of all the target commits "svn log" returned
            target_revs = []
            target_revs_rmndr = []
            for log_entry in target_log_entries:
                target_rev_tmp = log_entry['revision']
                ui.status("  verify_commit: target_revs.append(%s)", target_rev_tmp, level=ui.DEBUG, color='GREEN_B')
                target_revs.append(target_rev_tmp)
                target_revs_rmndr.append(target_rev_tmp)
            # Compare the two lists
            for d in source_revs:
                working_path   = d['path']
                source_rev_tmp = d['revision']
                target_rev_tmp = get_rev_map(source_rev_tmp, "  ")
                working_offset = working_path[len(source_base):].strip("/")
                sum1 = run_shell_command("svn cat -r %s '%s' | md5sum" % (source_rev_tmp, source_repos_url+working_path+"@"+str(source_rev_tmp)))
                sum2 = run_shell_command("svn cat -r %s '%s' | md5sum" % (target_rev_tmp, target_url+"/"+working_offset+"@"+str(target_rev_tmp))) if target_rev_tmp is not None else ""
                #print "source@%s: %s" % (str(source_rev_tmp).ljust(6), sum1)
                #print "target@%s: %s" % (str(target_rev_tmp).ljust(6), sum2)
                ui.status("  verify_commit: %s: source=%s target=%s", working_offset, source_rev_tmp, target_rev_tmp, level=ui.DEBUG, color='GREEN')
                if not target_rev_tmp:
                    ui.status(" (%s/%s) Verify path: FAIL: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA, color='RED')
                    ui.status("VerificationError: Unable to find corresponding target_rev for source_rev r%s in rev_map (path_offset='%s')", source_rev_tmp, path_offset, color='RED')
                    error_cnt +=1
                    continue
                if target_rev_tmp not in target_revs:
                    # If found a source_rev with no equivalent target_rev in target_revs,
                    # check if the only difference in source_rev vs. source_rev-1 is the
                    # removal/addition of a trailing newline char, since this seems to get
                    # stripped-out sometimes during the replay (via "svn export"?).
                    # Strip any trailing \r\n from file-content (http://stackoverflow.com/a/1656218/346778)
                    sum1 = run_shell_command("svn cat -r %s '%s' | perl -i -p0777we's/\\r\\n\z//' | md5sum" % (source_rev_tmp,   source_repos_url+working_path+"@"+str(source_rev_tmp)))
                    sum2 = run_shell_command("svn cat -r %s '%s' | perl -i -p0777we's/\\r\\n\z//' | md5sum" % (source_rev_tmp-1, source_repos_url+working_path+"@"+str(source_rev_tmp-1)))
                    if sum1 <> sum2:
                        ui.status(" (%s/%s) Verify path: FAIL: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA, color='RED')
                        ui.status("VerificationError: Found source_rev (r%s) with no corresponding target_rev: path_offset='%s'", source_rev_tmp, path_offset, color='RED')
                        error_cnt +=1
                    continue
                target_revs_rmndr.remove(target_rev_tmp)
            if target_revs_rmndr:
                rmndr_list = ", ".join(map(str, target_revs_rmndr))
                ui.status(" (%s/%s) Verify path: FAIL: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA, color='RED')
                ui.status("VerificationError: Found one or more *extra* target_revs: path_offset='%s', target_revs='%s'", path_offset, rmndr_list, color='RED')
                error_cnt +=1
            else:
                ui.status(" (%s/%s) Verify path: OK: %s", str(count).rjust(len(str(count_total))), count_total, path_offset, level=ui.EXTRA)

    # Ensure there are no "extra" files in the target side
    if options.verify == 2:
        target_paths = []
        child_paths = svnclient.list(target_url, target_rev, recursive=True)
        for p in child_paths:
            child_path_is_dir = True if p['kind'] == 'dir' else False
            child_path_offset = p['path']
            if not child_path_is_dir:
                target_paths.append(child_path_offset)
        # Compare
        for path_offset in target_paths:
            if not path_offset in check_paths:
                ui.status("VerificationError: Path exists in target (@%s) but not source (@%s): %s", target_rev, source_rev, path_offset, color='RED')
                error_cnt += 1
        for path_offset in check_paths:
            if not path_offset in target_paths:
                ui.status("VerificationError: Path exists in source (@%s) but not target (@%s): %s", source_rev, target_rev, path_offset, color='RED')
                error_cnt += 1

    if error_cnt > 0:
        raise VerificationError("Found %s verification errors" % (error_cnt))
    ui.status("Verified revision %s (%s).", target_rev, "all" if options.verify == 2 else "only-changed")

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
                    shell.rmtree(path)

def gen_tracking_revprops(source_rev):
    """
    Build an array of svn2svn-specific source-tracking revprops.
    """
    revprops = [{'name':'svn2svn:source_uuid', 'value':source_repos_uuid},
                {'name':'svn2svn:source_url',  'value':urllib.quote(source_url, ":/")},
                {'name':'svn2svn:source_rev',  'value':source_rev}]
    return revprops

def sync_svn_props(source_url, source_rev, path_offset):
    """
    Carry-forward any unversioned properties from the source repo to the
    target WC.
    """
    source_props = svnclient.propget_all(join_path(source_url, path_offset),  source_rev)
    target_props = svnclient.propget_all(path_offset)
    if 'svn:mergeinfo' in source_props:
        # Never carry-forward "svn:mergeinfo"
        del source_props['svn:mergeinfo']
    for prop in target_props:
        if prop not in source_props:
            # Remove any properties which exist in target but not source
            run_svn(["propdel", prop, svnclient.safe_path(path_offset)])
    for prop in source_props:
        if prop not in target_props or \
           source_props[prop] != target_props[prop]:
            # Set/update any properties which exist in source but not target or
            # whose value differs between source vs. target.
            run_svn(["propset", prop, source_props[prop], svnclient.safe_path(path_offset)])

def get_rev_map(source_rev, prefix):
    """
    Find the equivalent rev # in the target repo for the given rev # from the source repo.
    """
    ui.status(prefix + ">> get_rev_map(%s)", source_rev, level=ui.DEBUG, color='GREEN')
    # Find the highest entry less-than-or-equal-to source_rev
    for rev in range(int(source_rev), 0, -1):
        in_rev_map = True if rev in rev_map else False
        ui.status(prefix + ">> get_rev_map: rev=%s  in_rev_map=%s", rev, str(in_rev_map), level=ui.DEBUG, color='BLACK_B')
        if in_rev_map:
            return int(rev_map[rev])
    # Else, we fell off the bottom of the rev_map. Ruh-roh...
    return None

def set_rev_map(source_rev, target_rev):
    #ui.status(">> set_rev_map: source_rev=%s target_rev=%s", source_rev, target_rev, level=ui.DEBUG, color='GREEN')
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
               revprops['svn2svn:source_url'] == urllib.quote(source_info['url'], ":/"):
                source_rev = revprops['svn2svn:source_rev']
                target_rev = log_entry['revision']
                set_rev_map(source_rev, target_rev)
                proc_count += 1
                if proc_count % 500 == 0:
                    ui.status("...processed %s (%s of %s)..." % (proc_count, target_rev, target_end_rev), level=ui.VERBOSE)

def path_in_list(paths, path):
    for p in paths:
        if is_child_path(path, p):
            return True
    return False

def add_path(paths, path):
    if not path_in_list(paths, path):
        paths.append(path)

def in_ancestors(ancestors, ancestor):
    match = True
    for idx in range(len(ancestors)-1, 0, -1):
        if int(ancestors[idx]['revision']) > ancestor['revision']:
            match = is_child_path(ancestor['path'], ancestors[idx]['path'])
            break
    return match

def do_svn_add(source_url, path_offset, source_rev, source_ancestors, \
               parent_copyfrom_path="", parent_copyfrom_rev="", \
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
    and and add of /trunk/Proj2 copy-from /branches/fix1/Proj2. If we just did
    a straight "svn export+add" based on the /branches/fix1/Proj2 folder, we'd
    lose the logical history that Proj2/file2.txt is really a descendant of
    Proj1/file1.txt.

    'path_offset' is the offset from source_base to the file to check ancestry for,
      e.g. 'projectA/file1.txt'. path = source_repos_url + source_base + path_offset.
    'source_rev' is the revision ("svn log") that we're processing from the source repo.
    'parent_copyfrom_path' and 'parent_copyfrom_rev' is the copy-from path of the parent
      directory, when being called recursively by do_svn_add_dir().
    'export_paths' is the list of path_offset's that we've deferred running "svn export" on.
    'is_dir' is whether path_offset is a directory (rather than a file).
    """
    source_base = source_url[len(source_repos_url):]  # e.g. '/trunk'
    ui.status(prefix + ">> do_svn_add: %s  %s", join_path(source_base, path_offset)+"@"+str(source_rev),
        "  (parent-copyfrom: "+parent_copyfrom_path+"@"+str(parent_copyfrom_rev)+")" if parent_copyfrom_path else "",
        level=ui.DEBUG, color='GREEN')
    # Check if the given path has ancestors which chain back to the current source_base
    found_ancestor = False
    ancestors = find_svn_ancestors(source_repos_url, join_path(source_base, path_offset), source_rev, stop_base_path=source_base, prefix=prefix+"  ")
    ancestor = ancestors[len(ancestors)-1] if ancestors else None  # Choose the eldest ancestor, i.e. where we reached stop_base_path=source_base
    if ancestor and not in_ancestors(source_ancestors, ancestor):
        ancestor = None
    copyfrom_path = ancestor['copyfrom_path'] if ancestor else ""
    copyfrom_rev  = ancestor['copyfrom_rev']  if ancestor else ""
    if ancestor:
        # The copy-from path has ancestry back to source_url.
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
                    ui.status(" %s %s (from %s)", ('R' if path_in_svn else 'A'), join_path(source_base, path_offset), ancestors[0]['copyfrom_path']+"@"+str(copyfrom_rev), level=ui.VERBOSE)
                if path_in_svn:
                    # If local file is already under version-control, then this is a replace.
                    ui.status(prefix + ">> do_svn_add: pre-copy: local path already exists: %s", path_offset, level=ui.DEBUG, color='GREEN')
                    svnclient.update(path_offset)
                    svnclient.remove(path_offset, force=True)
                run_svn(["copy", "-r", tgt_rev, svnclient.safe_path(join_path(target_url, copyfrom_offset), tgt_rev), svnclient.safe_path(path_offset)])
                if is_dir:
                    # Export the final verison of all files in this folder.
                    add_path(export_paths, path_offset)
                else:
                    # Export the final verison of this file.
                    svnclient.export(source_repos_url+join_path(source_base, path_offset), source_rev, path_offset, force=True)
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
        p_path = path_offset if is_dir else os.path.dirname(path_offset).strip() or None
        if p_path and not os.path.exists(p_path):
            run_svn(["mkdir", svnclient.safe_path(p_path)])
        if not in_svn(path_offset, prefix=prefix+"  "):
            if is_dir:
                # Export the final verison of all files in this folder.
                add_path(export_paths, path_offset)
            else:
                # Export the final verison of this file. We *need* to do this before running
                # the "svn add", even if we end-up re-exporting this file again via export_paths.
                svnclient.export(source_repos_url+join_path(source_base, path_offset), source_rev, path_offset, force=True)
            # If not already under version-control, then "svn add" this file/folder.
            run_svn(["add", "--parents", svnclient.safe_path(path_offset)])
        if options.keep_prop:
            sync_svn_props(source_url, source_rev, path_offset)
    if is_dir:
        # For any folders that we process, process any child contents, so that we correctly
        # replay copies/replaces/etc.
        do_svn_add_dir(source_url, path_offset, source_rev, source_ancestors,
                       copyfrom_path, copyfrom_rev, export_paths, skip_paths, prefix+"  ")

def do_svn_add_dir(source_url, path_offset, source_rev, source_ancestors, \
                   parent_copyfrom_path, parent_copyfrom_rev, \
                   export_paths, skip_paths, prefix=""):
    source_base = source_url[len(source_repos_url):]  # e.g. '/trunk'
    # Get the directory contents, to compare between the local WC (target_url) vs. the remote repo (source_url)
    # TODO: paths_local won't include add'd paths because "svn ls" lists the contents of the
    #       associated remote repo folder. (Is this a problem?)
    paths_local =  svnclient.list(path_offset)
    paths_remote = svnclient.list(join_path(source_url, path_offset), source_rev)
    ui.status(prefix + ">> do_svn_add_dir: paths_local:  %s", str(paths_local),  level=ui.DEBUG, color='GREEN')
    ui.status(prefix + ">> do_svn_add_dir: paths_remote: %s", str(paths_remote), level=ui.DEBUG, color='GREEN')
    # Update files/folders which exist in remote but not local
    for p in paths_remote:
        path_is_dir = True if p['kind'] == 'dir' else False
        working_path = join_path(path_offset, p['path']).lstrip('/')
        #print "working_path:%s = path_offset:%s + path:%s" % (working_path, path_offset, path)
        if not working_path in skip_paths:
            do_svn_add(source_url, working_path, source_rev, source_ancestors,
                       parent_copyfrom_path, parent_copyfrom_rev,
                       export_paths, path_is_dir, skip_paths, prefix+"  ")
    # Remove files/folders which exist in local but not remote
    for p in paths_local:
        if not p in paths_remote:
            working_path = join_path(path_offset, p['path']).lstrip('/')
            ui.status(" %s %s", 'D', join_path(source_base, working_path), level=ui.VERBOSE)
            svnclient.update(working_path)
            svnclient.remove(working_path, force=True)
            # TODO: Does this handle deleted folders too? Wouldn't want to have a case
            #       where we only delete all files from folder but leave orphaned folder around.

def process_svn_log_entry(log_entry, ancestors, commit_paths, prefix = ""):
    """
    Process SVN changes from the given log entry. Build an array (commit_paths)
    of the paths in the working-copy that were changed, i.e. the paths which
    we'll pass to "svn commit".
    """
    export_paths = []
    source_rev = log_entry['revision']
    source_url = log_entry['url']
    source_base = source_url[len(source_repos_url):]  # e.g. '/trunk'
    ui.status(prefix + ">> process_svn_log_entry: %s", source_url+"@"+str(source_rev), level=ui.DEBUG, color='GREEN')
    for d in log_entry['changed_paths']:
        # Get the full path for this changed_path
        # e.g. '/branches/bug123/projectA/file1.txt'
        path = d['path']
        if not is_child_path(path, source_base):
            # Ignore changed files that are not part of this subdir
            ui.status(prefix + ">> process_svn_log_entry: Unrelated path: %s  (base: %s)", path, source_base, level=ui.DEBUG, color='GREEN')
            continue
        if d['kind'] == "" or d['kind'] == 'none':
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
        if action not in svnclient.valid_svn_actions:
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
            if path_offset and in_svn(path_offset):
                # Target path might not be under version-control yet, e.g. parent "add"
                # was a copy-from a branch which had no ancestry back to trunk, and each
                # child folder under that parent folder is a "replace" action on the final
                # merge to trunk. Since the child folders will be in skip_paths, do_svn_add
                # wouldn't have created them while processing the parent "add" path.
                if path_is_dir:
                    # Need to "svn update" before "svn remove" in case child contents are at
                    # a higher rev than the (parent) path_offset.
                    svnclient.update(path_offset)
                svnclient.remove(path_offset, force=True)
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
                    if is_child_path(tmp_path, path) and tmp_d['action'] in 'ARD':
                        # Build list of child entries which are also in the changed_paths list,
                        # so that do_svn_add() can skip processing these entries when recursing
                        # since we'll end-up processing them later. Don't include action="M" paths
                        # in this list because it's non-conclusive: it could just mean that the
                        # file was modified *after* the copy-from, so we still want do_svn_add()
                        # to re-create the correct ancestry.
                        tmp_path_offset = tmp_path[len(source_base):].strip("/")
                        skip_paths.append(tmp_path_offset)
                do_svn_add(source_url, path_offset, source_rev, ancestors, "", "", export_paths, path_is_dir, skip_paths, prefix+"  ")
            # Else just "svn export" the files from the source repo and "svn add" them.
            else:
                # Create (parent) directory if needed
                p_path = path_offset if path_is_dir else os.path.dirname(path_offset).strip() or None
                if p_path and not os.path.exists(p_path):
                    run_svn(["mkdir", svnclient.safe_path(p_path)])
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
                    svnclient.export(join_path(source_url, path_offset), source_rev, path_offset, force=True)
                if not in_svn(path_offset, prefix=prefix+"  "):
                    # Need to use in_svn here to handle cases where client committed the parent
                    # folder and each indiv sub-folder.
                    run_svn(["add", "--parents", svnclient.safe_path(path_offset)])
                if options.keep_prop:
                    sync_svn_props(source_url, source_rev, path_offset)

        elif action == 'D':
            if path_is_dir:
                # For dirs, need to "svn update" before "svn remove" because the final
                # "svn commit" will fail if the parent (path_offset) is at a lower rev
                # than any of the child contents. This needs to be a recursive update.
                svnclient.update(path_offset)
            svnclient.remove(path_offset, force=True)

        elif action == 'M':
            if path_is_file:
                svnclient.export(join_path(source_url, path_offset), source_rev, path_offset, force=True, non_recursive=True)
            if path_is_dir:
                # For dirs, need to "svn update" before export/prop-sync because the
                # final "svn commit" will fail if the parent is at a lower rev than
                # child contents. Just need to update the rev-state of the dir (d['path']),
                # don't need to recursively update all child contents.
                # (??? is this the right reason?)
                svnclient.update(path_offset, non_recursive=True)
            if options.keep_prop:
                sync_svn_props(source_url, source_rev, path_offset)

        else:
            raise InternalError("Internal Error: process_svn_log_entry: Unhandled 'action' value: '%s'"
                % action)

    # Export the final version of all add'd paths from source_url
    if export_paths:
        for path_offset in export_paths:
            svnclient.export(join_path(source_url, path_offset), source_rev, path_offset, force=True)

def keep_revnum(source_rev, target_rev_last, wc_target_tmp):
    """
    Add "padding" target revisions as needed to keep source and target
    revision #'s identical.
    """
    bh = BreakHandler()
    if int(source_rev) <= int(target_rev_last):
        raise InternalError("keep-revnum mode is enabled, "
            "but source revision (r%s) is less-than-or-equal last target revision (r%s)" % \
            (source_rev, target_rev_last))
    if int(target_rev_last) < int(source_rev)-1:
        # Add "padding" target revisions to keep source and target rev #'s identical
        if os.path.exists(wc_target_tmp):
            shell.rmtree(wc_target_tmp)
        run_svn(["checkout", "-r", "HEAD", "--depth=empty", svnclient.safe_path(target_repos_url, "HEAD"), svnclient.safe_path(wc_target_tmp)])
        for rev_num in range(int(target_rev_last)+1, int(source_rev)):
            run_svn(["propset", "svn2svn:keep-revnum", rev_num, svnclient.safe_path(wc_target_tmp)])
            # Prevent Ctrl-C's during this inner part, so we'll always display
            # the "Commit revision ..." message if we ran a "svn commit".
            bh.enable()
            output = run_svn(["commit", "-m", "", svnclient.safe_path(wc_target_tmp)])
            rev_num_tmp = parse_svn_commit_rev(output) if output else None
            assert rev_num == rev_num_tmp
            ui.status("Committed revision %s (keep-revnum).", rev_num)
            bh.disable()
            # Check if the user tried to press Ctrl-C
            if bh.trapped:
                raise KeyboardInterrupt
            target_rev_last = rev_num
        shell.rmtree(wc_target_tmp)
    return target_rev_last

def disp_svn_log_summary(log_entry):
    ui.status("------------------------------------------------------------------------", level=ui.VERBOSE)
    ui.status("r%s | %s | %s",
        log_entry['revision'],
        log_entry['author'],
        str(datetime.fromtimestamp(int(log_entry['date'])).isoformat(' ')), level=ui.VERBOSE)
    ui.status(log_entry['message'], level=ui.VERBOSE)

def real_main(args):
    global source_url, target_url, rev_map
    # Use urllib.unquote() to URL-decode source_url/target_url values.
    # All URLs passed to run_svn() should go through svnclient.safe_path()
    # and we don't want to end-up *double* urllib.quote'ing if the user-
    # supplied source/target URL's are already URL-encoded.
    source_url = urllib.unquote(args.pop(0).rstrip("/"))   # e.g. 'http://server/svn/source/trunk'
    target_url = urllib.unquote(args.pop(0).rstrip("/"))   # e.g. 'file:///svn/target/trunk'
    ui.status("options: %s", str(options), level=ui.DEBUG, color='GREEN')

    # Make sure that both the source and target URL's are valid
    source_info = svnclient.info(source_url)
    assert is_child_path(source_url, source_info['repos_url'])
    target_info = svnclient.info(target_url)
    assert is_child_path(target_url, target_info['repos_url'])

    # Init global vars
    global source_repos_url,source_base,source_repos_uuid
    source_repos_url = source_info['repos_url']       # e.g. 'http://server/svn/source'
    source_base = source_url[len(source_repos_url):]  # e.g. '/trunk'
    source_repos_uuid = source_info['repos_uuid']
    global target_repos_url,target_base
    target_repos_url = target_info['repos_url']       # e.g. 'http://server/svn/target'
    target_base = target_url[len(target_repos_url):]  # e.g. '/trunk'

    # Init start and end revision
    try:
        source_start_rev = svnclient.get_rev(source_repos_url, options.rev_start if options.rev_start else 1)
    except ExternalCommandFailed:
        print "Error: Invalid start source revision value: %s" % (options.rev_start)
        return 1
    try:
        source_end_rev   = svnclient.get_rev(source_repos_url, options.rev_end   if options.rev_end   else "HEAD")
    except ExternalCommandFailed:
        print "Error: Invalid end source revision value: %s" % (options.rev_end)
        return 1
    ui.status("Using source revision range %s:%s", source_start_rev, source_end_rev, level=ui.VERBOSE)

    # TODO: If options.keep_date, should we try doing a "svn propset" on an *existing* revision
    #       as a sanity check, so we check if the pre-revprop-change hook script is correctly setup
    #       before doing first replay-commit?

    target_rev_last =  target_info['revision']   # Last revision # in the target repo
    wc_target = os.path.abspath('_wc_target')
    wc_target_tmp = os.path.abspath('_wc_target_tmp')
    num_entries_proc = 0
    commit_count = 0
    source_rev = None
    target_rev = None

    # Check out a working copy of target_url if needed
    wc_exists = os.path.exists(wc_target)
    if wc_exists and not options.cont_from_break:
        shell.rmtree(wc_target)
        wc_exists = False
    if not wc_exists:
        ui.status("Checking-out _wc_target...", level=ui.VERBOSE)
        svnclient.svn_checkout(target_url, wc_target)
    os.chdir(wc_target)
    if wc_exists:
        # If using an existing WC, make sure it's clean ("svn revert")
        ui.status("Cleaning-up _wc_target...", level=ui.VERBOSE)
        run_svn(["cleanup"])
        full_svn_revert()

    if not options.cont_from_break:
        # Warn user if trying to start (non-continue) into a non-empty target path
        if not options.force_nocont:
            top_paths = svnclient.list(target_url, "HEAD")
            if len(top_paths)>0:
                print "Error: Trying to replay (non-continue-mode) into a non-empty target_url location. " \
                      "Use --force if you're sure this is what you want."
                return 1
        # Get the first log entry at/after source_start_rev, which is where
        # we'll do the initial import from.
        source_ancestors = find_svn_ancestors(source_repos_url, source_base, source_end_rev, prefix="  ")
        it_log_start = svnclient.iter_svn_log_entries(source_url, source_start_rev, source_end_rev, get_changed_paths=False, ancestors=source_ancestors)
        source_start_log = None
        for log_entry in it_log_start:
            # Pick the first entry. Need to use a "for ..." loop since we're using an iterator.
            source_start_log = log_entry
            break
        if not source_start_log:
            raise InternalError("Unable to find any matching revisions between %s:%s in source_url: %s" % \
                (source_start_rev, source_end_rev, source_url))

        # This is the revision we will start from for source_url
        source_start_rev = int(source_start_log['revision'])
        ui.status("Starting at source revision %s.", source_start_rev, level=ui.VERBOSE)
        ui.status("", level=ui.VERBOSE)
        if options.keep_revnum and source_rev > target_rev_last:
            target_rev_last = keep_revnum(source_rev, target_rev_last, wc_target_tmp)

        # For the initial commit to the target URL, export all the contents from
        # the source URL at the start-revision.
        disp_svn_log_summary(svnclient.get_one_svn_log_entry(source_repos_url, source_start_rev, source_start_rev))
        # Export and add file-contents from source_url@source_start_rev
        source_start_url = source_url if not source_ancestors else source_repos_url+source_ancestors[len(source_ancestors)-1]['copyfrom_path']
        top_paths = svnclient.list(source_start_url, source_start_rev)
        for p in top_paths:
            # For each top-level file/folder...
            path_is_dir = True if p['kind'] == "dir" else False
            path_offset = p['path']
            if in_svn(path_offset, prefix="  "):
                raise InternalError("Cannot replay history on top of pre-existing structure: %s" % join_path(source_start_url, path_offset))
            if path_is_dir and not os.path.exists(path_offset):
                os.makedirs(path_offset)
            svnclient.export(join_path(source_start_url, path_offset), source_start_rev, path_offset, force=True)
            run_svn(["add", svnclient.safe_path(path_offset)])
        # Update any properties on the newly added content
        paths = svnclient.list(source_start_url, source_start_rev, recursive=True)
        if options.keep_prop:
            sync_svn_props(source_start_url, source_start_rev, "")
        for p in paths:
            path_offset = p['path']
            ui.status(" A %s", join_path(source_base, path_offset), level=ui.VERBOSE)
            if options.keep_prop:
                sync_svn_props(source_start_url, source_start_rev, path_offset)
        # Commit the initial import
        num_entries_proc += 1
        target_revprops = gen_tracking_revprops(source_start_rev)   # Build source-tracking revprop's
        target_rev = commit_from_svn_log_entry(source_start_log, target_revprops=target_revprops)
        if target_rev:
            # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
            set_rev_map(source_start_rev, target_rev)
            commit_count += 1
            target_rev_last = target_rev
            if options.verify:
                verify_commit(source_rev, target_rev_last)
    else:
        # Re-build the rev_map based on any already-replayed history in target_url
        build_rev_map(target_url, target_rev_last, source_info)
        if not rev_map:
            print "Error: Called with continue-mode, but no already-replayed source history found in target_url."
            return 1
        source_start_rev = int(max(rev_map, key=rev_map.get))
        assert source_start_rev
        ui.status("Continuing from source revision %s.", source_start_rev, level=ui.VERBOSE)
        ui.status("", level=ui.VERBOSE)

    svn_vers_t = svnclient.version()
    svn_vers = float(".".join(map(str, svn_vers_t[0:2])))

    # Load SVN log starting from source_start_rev + 1
    source_ancestors = find_svn_ancestors(source_repos_url, source_base, source_end_rev, prefix="  ")
    it_log_entries = svnclient.iter_svn_log_entries(source_url, source_start_rev+1, source_end_rev, get_revprops=True, ancestors=source_ancestors) if source_start_rev < source_end_rev else []
    source_rev_last = source_start_rev
    exit_code = 0

    try:
        for log_entry in it_log_entries:
            if options.entries_proc_limit:
                if num_entries_proc >= options.entries_proc_limit:
                    break
            # Replay this revision from source_url into target_url
            source_rev = log_entry['revision']
            log_url =    log_entry['url']
            #print "source_url:%s  log_url:%s" % (source_url, log_url)
            if options.keep_revnum:
                if source_rev < target_rev_last:
                    print "Error: Last target revision (r%s) is equal-or-higher than starting source revision (r%s). " \
                        "Cannot use --keep-revnum mode." % (target_rev_last, source_start_rev)
                    return 1
                target_rev_last = keep_revnum(source_rev, target_rev_last, wc_target_tmp)
            disp_svn_log_summary(log_entry)
            # Process all the changed-paths in this log entry
            commit_paths = []
            process_svn_log_entry(log_entry, source_ancestors, commit_paths)
            num_entries_proc += 1
            # Commit any changes made to _wc_target
            target_revprops = gen_tracking_revprops(source_rev)   # Build source-tracking revprop's
            target_rev = commit_from_svn_log_entry(log_entry, commit_paths, target_revprops=target_revprops)
            source_rev_last = source_rev
            if target_rev:
                # Update rev_map, mapping table of source-repo rev # -> target-repo rev #
                source_rev = log_entry['revision']
                set_rev_map(source_rev, target_rev)
                target_rev_last = target_rev
                commit_count += 1
                if options.verify:
                    verify_commit(source_rev, target_rev_last, log_entry)
                # Run "svn cleanup" every 100 commits if SVN 1.7+, to clean-up orphaned ".svn/pristines/*"
                if svn_vers >= 1.7 and (commit_count % 100 == 0):
                    run_svn(["cleanup"])
        if source_rev_last == source_start_rev:
            # If there were no new source_url revisions to process, still trigger
            # "full-mode" verify check (if enabled).
            if options.verify:
                verify_commit(source_rev_last, target_rev_last)

    except KeyboardInterrupt:
        exit_code = 1
        print "\nStopped by user."
        print "\nCleaning-up..."
        run_svn(["cleanup"])
        full_svn_revert()
    except:
        exit_code = 1
        print "\nCommand failed with following error:\n"
        traceback.print_exc()
        print "\nCleaning-up..."
        run_svn(["cleanup"])
        print run_svn(["status"])
        full_svn_revert()
    finally:
        print "\nFinished at source revision %s%s." % (source_rev_last, " (dry-run)" if options.dry_run else "")

    return exit_code

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
  $ svnreplay -av -r 5000 http://server/source/trunk file:///svn/target/trunk
    1. The target_url will be checked-out to ./_wc_target
    2. The first commit to http://server/source/trunk at/after r5000 will be
       exported & added into _wc_target
    3. All revisions affecting http://server/source/trunk (starting at r5000)
       will be replayed to _wc_target. Any add/copy/move/replaces that are
       copy-from'd some path outside of /trunk (e.g. files renamed on a
       /branch and branch was merged into /trunk) will correctly maintain
       logical ancestry where possible.

  Use continue-mode (-c) to pick-up where the last run left-off
  $ svnreplay -avc http://server/source/trunk file:///svn/target/trunk
    1. The target_url will be checked-out to ./_wc_target, if not already
       checked-out
    2. All new revisions affecting http://server/source/trunk starting from
       the last replayed revision to file:///svn/target/trunk (based on the
       svn2svn:* revprops) will be replayed to _wc_target, maintaining all
       logical ancestry where possible."""
    parser = optparse.OptionParser(usage, description=description,
                formatter=HelpFormatter(), version="%prog "+str(full_version))
    parser.add_option("-v", "--verbose", dest="verbosity", action="count", default=1,
                      help="Enable additional output (use -vv or -vvv for more).")
    parser.add_option("-a", "--archive", action="store_true", dest="archive", default=False,
                      help="Archive/mirror mode; same as -UDP (see REQUIRES below).\n"
                           "Maintain same commit author, same commit time, and file/dir properties.")
    parser.add_option("-U", "--keep-author", action="store_true", dest="keep_author", default=False,
                      help="Maintain same commit authors (svn:author) as source.\n"
                           "(REQUIRES 'pre-revprop-change' hook script to allow 'svn:author' changes.)")
    parser.add_option("-D", "--keep-date", action="store_true", dest="keep_date", default=False,
                      help="Maintain same commit time (svn:date) as source.\n"
                           "(REQUIRES 'pre-revprop-change' hook script to allow 'svn:date' changes.)")
    parser.add_option("-P", "--keep-prop", action="store_true", dest="keep_prop", default=False,
                      help="Maintain same file/dir SVN properties as source.")
    parser.add_option("-R", "--keep-revnum", action="store_true", dest="keep_revnum", default=False,
                      help="Maintain same rev #'s as source. Creates placeholder target "
                            "revisions (by modifying a 'svn2svn:keep-revnum' property at the root of the target repo).")
    parser.add_option("-c", "--continue", action="store_true", dest="cont_from_break",
                      help="Continue from last source commit to target (based on svn2svn:* revprops).")
    parser.add_option("-f", "--force", action="store_true", dest="force_nocont",
                      help="Allow replaying into a non-empty target-repo folder.")
    parser.add_option("-r", "--revision", type="string", dest="revision", metavar="ARG",
                      help="Revision range to replay from source_url.\n"
                           "A revision argument can be one of:\n"
                           "   START        Start rev # (end will be 'HEAD')\n"
                           "   START:END    Start and ending rev #'s\n"
                           "Any revision # formats which SVN understands are "
                           "supported, e.g. 'HEAD', '{2010-01-31}', etc.")
    parser.add_option("-u", "--log-author", action="store_true", dest="log_author", default=False,
                      help="Append source commit author to replayed commit mesages.")
    parser.add_option("-d", "--log-date", action="store_true", dest="log_date", default=False,
                      help="Append source commit time to replayed commit messages.")
    parser.add_option("-l", "--limit", type="int", dest="entries_proc_limit", metavar="NUM",
                      help="Maximum number of source revisions to process.")
    parser.add_option("-n", "--dry-run", action="store_true", dest="dry_run", default=False,
                      help="Process next source revision but don't commit changes to "
                           "target working-copy (forces --limit=1).")
    parser.add_option("-x", "--verify",     action="store_const", const=1, dest="verify",
                      help="Verify ancestry and content for changed paths in commit after every target commit or last target commit.")
    parser.add_option("-X", "--verify-all", action="store_const", const=2, dest="verify",
                      help="Verify ancestry and content for entire target_url tree after every target commit or last target commit.")
    parser.add_option("--pre-commit", type="string", dest="beforecommit", metavar="CMD",
                      help="Run the given shell script before each replayed commit, e.g. "
                           "to modify file-content during replay.\n"
                           "Called as: CMD [wc_path] [source_rev]")
    parser.add_option("--debug", dest="verbosity", const=ui.DEBUG, action="store_const",
                      help="Enable debugging output (same as -vvv).")
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
    return real_main(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
