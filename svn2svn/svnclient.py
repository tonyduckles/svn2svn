
from hgsvn import ui
from hgsvn.common import (run_svn, once_or_more)
from hgsvn.errors import EmptySVNLog

import os
import time
import calendar
import operator

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
svn_status_args = ['status', '--xml', '--ignore-externals']

_identity_table = "".join(map(chr, range(256)))
_forbidden_xml_chars = "".join(
    set(map(chr, range(32))) - set('\x09\x0A\x0D')
)


def strip_forbidden_xml_chars(xml_string):
    """
    Given an XML string, strips forbidden characters as per the XML spec.
    (these are all control characters except 0x9, 0xA and 0xD).
    """
    return xml_string.translate(_identity_table, _forbidden_xml_chars)


def svn_date_to_timestamp(svn_date):
    """
    Parse an SVN date as read from the XML output and return the corresponding
    timestamp.
    """
    # Strip microseconds and timezone (always UTC, hopefully)
    # XXX there are various ISO datetime parsing routines out there,
    # cf. http://seehuhn.de/comp/pdate
    date = svn_date.split('.', 2)[0]
    time_tuple = time.strptime(date, "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(time_tuple)

def parse_svn_info_xml(xml_string):
    """
    Parse the XML output from an "svn info" command and extract useful information
    as a dict.
    """
    d = {}
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    entry = tree.find('.//entry')
    d['url'] = entry.find('url').text
    d['revision'] = int(entry.get('revision'))
    d['repos_url'] = tree.find('.//repository/root').text
    d['last_changed_rev'] = int(tree.find('.//commit').get('revision'))
    author_element = tree.find('.//commit/author')
    if author_element is not None:
        d['last_changed_author'] = author_element.text
    d['last_changed_date'] = svn_date_to_timestamp(tree.find('.//commit/date').text)
    return d

def parse_svn_log_xml(xml_string):
    """
    Parse the XML output from an "svn log" command and extract useful information
    as a list of dicts (one per log changeset).
    """
    l = []
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('logentry'):
        d = {}
        d['revision'] = int(entry.get('revision'))
        # Some revisions don't have authors, most notably the first revision
        # in a repository.
        # logentry nodes targeting directories protected by path-based
        # authentication have no child nodes at all. We return an entry
        # in that case. Anyway, as it has no path entries, no further
        # processing will be made.
        author = entry.find('author')
        date = entry.find('date')
        msg = entry.find('msg')
        # Issue 64 - modified to prevent crashes on svn log entries with "No author"
        d['author'] = author is not None and author.text or "No author"
        if date is not None:
            d['date'] = svn_date_to_timestamp(date.text)
        else:
            d['date'] = None
        d['message'] = msg is not None and msg.text or ""
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

def parse_svn_status_xml(xml_string, base_dir=None, ignore_externals=False):
    """
    Parse the XML output from an "svn status" command and extract useful info
    as a list of dicts (one per status entry).
    """
    if base_dir:
        base_dir = os.path.normcase(base_dir)
    l = []
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('.//entry'):
        d = {}
        path = entry.get('path')
        if base_dir is not None:
            assert os.path.normcase(path).startswith(base_dir)
            path = path[len(base_dir):].lstrip('/\\')
        d['path'] = path
        wc_status = entry.find('wc-status')
        if wc_status.get('item') == 'external':
            if ignore_externals:
                continue
            d['type'] = 'external'
        elif wc_status.get('revision') is not None:
            d['type'] = 'normal'
        else:
            d['type'] = 'unversioned'
        d['status'] = wc_status.get('item')
        l.append(d)
    return l

def get_svn_info(svn_url_or_wc, rev_number=None):
    """
    Get SVN information for the given URL or working copy, with an optionally
    specified revision number.
    Returns a dict as created by parse_svn_info_xml().
    """
    if rev_number is not None:
        args = ['-r', rev_number]
    else:
        args = []
    xml_string = run_svn(svn_info_args + args + [svn_url_or_wc],
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

def run_svn_log(svn_url, rev_start, rev_end, limit, stop_on_copy=False):
    """
    Fetch up to 'limit' SVN log entries between the given revisions.
    """
    if stop_on_copy:
        args = ['--stop-on-copy']
    else:
        args = []
    args += ['-r', '%s:%s' % (rev_start, rev_end), '--limit', limit, svn_url]
    xml_string = run_svn(svn_log_args + args)
    return parse_svn_log_xml(xml_string)

def get_svn_status(svn_wc, quiet=False):
    """
    Get SVN status information about the given working copy.
    """
    # Ensure proper stripping by canonicalizing the path
    svn_wc = os.path.abspath(svn_wc)
    args = [svn_wc]
    if quiet:
        args += ['-q']
    else:
        args += ['-v']
    xml_string = run_svn(svn_status_args + args)
    return parse_svn_status_xml(xml_string, svn_wc, ignore_externals=True)

def get_svn_versioned_files(svn_wc):
    """
    Get the list of versioned files in the SVN working copy.
    """
    contents = []
    for e in get_svn_status(svn_wc):
        if e['path'] and e['type'] == 'normal':
            contents.append(e['path'])
    return contents


def get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=False):
    """
    Get the first SVN log entry in the requested revision range.
    """
    entries = run_svn_log(svn_url, rev_start, rev_end, 1, stop_on_copy)
    if entries:
        return entries[0]
    raise EmptySVNLog("No SVN log for %s between revisions %s and %s" %
        (svn_url, rev_start, rev_end))


def get_first_svn_log_entry(svn_url, rev_start, rev_end):
    """
    Get the first log entry after (or at) the given revision number in an SVN branch.
    By default the revision number is set to 0, which will give you the log
    entry corresponding to the branch creaction.

    NOTE: to know whether the branch creation corresponds to an SVN import or
    a copy from another branch, inspect elements of the 'changed_paths' entry
    in the returned dictionary.
    """
    return get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=True)

def get_last_svn_log_entry(svn_url, rev_start, rev_end):
    """
    Get the last log entry before (or at) the given revision number in an SVN branch.
    By default the revision number is set to HEAD, which will give you the log
    entry corresponding to the latest commit in branch.
    """
    return get_one_svn_log_entry(svn_url, rev_end, rev_start, stop_on_copy=True)


log_duration_threshold = 10.0
log_min_chunk_length = 10

def iter_svn_log_entries(svn_url, first_rev, last_rev, retry):
    """
    Iterate over SVN log entries between first_rev and last_rev.

    This function features chunked log fetching so that it isn't too nasty
    to the SVN server if many entries are requested.
    """
    cur_rev = first_rev
    chunk_length = log_min_chunk_length
    first_run = True
    while last_rev == "HEAD" or cur_rev <= last_rev:
        start_t = time.time()
        stop_rev = min(last_rev, cur_rev + chunk_length)
        ui.status("Fetching %s SVN log entries starting from revision %d...",
                  chunk_length, cur_rev, level=ui.VERBOSE)
        entries = once_or_more("Fetching SVN log", retry, run_svn_log, svn_url,
                               cur_rev, "HEAD", chunk_length)
        duration = time.time() - start_t
        if not first_run:
            # skip first revision on subsequent runs, as it is overlapped
            entries.pop(0)
        first_run = False
        if not entries:
            break
        for e in entries:
            if e['revision'] > last_rev:
                break
            yield e
        if e['revision'] >= last_rev:
            break
        cur_rev = e['revision']
        # Adapt chunk length based on measured request duration
        if duration < log_duration_threshold:
            chunk_length = int(chunk_length * 2.0)
        elif duration > log_duration_threshold * 2:
            chunk_length = max(log_min_chunk_length, int(chunk_length / 2.0))


_svn_client_version = None

def get_svn_client_version():
    """Returns the SVN client version as a tuple.

    The returned tuple only contains numbers, non-digits in version string are
    silently ignored.
    """
    global _svn_client_version
    if _svn_client_version is None:
        raw = run_svn(['--version', '-q']).strip()
        _svn_client_version = tuple(map(int, [x for x in raw.split('.')
                                              if x.isdigit()]))
    return _svn_client_version
