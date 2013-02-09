"""
Display ancestry for a given path in an SVN repository.
"""

from svn2svn import base_version, full_version
from svn2svn import ui
from svn2svn import svnclient
from parse import HelpFormatter
from svn2svn.run.common import find_svn_ancestors

import optparse
import re

options = None

def real_main(args):
    global options
    url = args.pop(0)
    ui.status("url: %s", url, level=ui.DEBUG, color='GREEN')
    info = svnclient.info(url)
    repos_root = info['repos_url']
    repos_path = url[len(repos_root):]
    ancestors = find_svn_ancestors(repos_root, repos_path, options.revision)
    if ancestors:
        max_len = 0
        for idx in range(len(ancestors)):
            d = ancestors[idx]
            max_len = max(max_len, len(d['path']+"@"+str(d['revision'])))
        for idx in range(len(ancestors)):
            d = ancestors[idx]
            ui.status("[%s] %s --> %s", len(ancestors)-idx-1,
                str(d['path']+"@"+str(d['revision'])).ljust(max_len),
                str(d['copyfrom_path']+"@"+str(d['copyfrom_rev'])))
    else:
        ui.status("No ancestor-chain found: %s", repos_root+repos_path+"@"+str(options.revision))

    return 0

def main():
    # Defined as entry point. Must be callable without arguments.
    usage = "svn2svn, version %s\n" % str(full_version) + \
            "<http://nynim.org/code/svn2svn> <https://github.com/tonyduckles/svn2svn>\n\n" + \
            "Usage: %prog [OPTIONS] url\n"
    description = """\
Display ancestry for a given path in an SVN repository."""
    parser = optparse.OptionParser(usage, description=description,
                formatter=HelpFormatter(), version="%prog "+str(full_version))
    parser.add_option("-v", "--verbose", dest="verbosity", action="count", default=1,
                      help="enable additional output (use -vv or -vvv for more)")
    parser.add_option("-r", "--revision", type="string", dest="revision", metavar="ARG",
                      help="revision range to replay from source_url\n"
                           "Any revision # formats which SVN understands are "
                           "supported, e.g. 'HEAD', '{2010-01-31}', etc.")
    parser.add_option("--debug", dest="verbosity", const=ui.DEBUG, action="store_const",
                      help="enable debugging output (same as -vvv)")
    global options
    options, args = parser.parse_args()
    if len(args) != 1:
        parser.error("incorrect number of arguments")
    if options.verbosity < 10:
        # Expand multiple "-v" arguments to a real ui._level value
        options.verbosity *= 10
    if options.revision:
        # Reg-ex for matching a revision arg (http://svnbook.red-bean.com/en/1.5/svn.tour.revs.specifiers.html#svn.tour.revs.dates)
        rev_patt = '[0-9A-Z]+|\{[0-9A-Za-z/\\ :-]+\}'
        rev = None
        match = re.match('^('+rev_patt+')$', options.revision)
        if match is None:
            parser.error("unexpected --revision argument format; see 'svn help log' for valid revision formats")
        rev = match.groups()
        options.revision = rev[0] if len(rev)>0 else None
    else:
        options.revision = 'HEAD'
    ui.update_config(options)
    return real_main(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
