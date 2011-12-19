## Svn2svn

`svn2svn` is a utility for replicating the revision history from a source path
in a source SVN repository to a target path in a target SVN repository. In other
words, it "replays the history" of a given SVN folder into a new SVN repository.

This can be useful to create filtered version of a source SVN repository. For example,
say that you have a huge Subversion repository with a _lot_ of old branch history
which is taking up a lot of disk-space and not serving a lot of purpose going forward.
You can this utility to replay/filter just the "/trunk" SVN history into a new history,
so that things like "svn log" and "svn blame" will still show the (logically) correct
history, even though we end-up generating new commits and hence have new commit dates.

The original commit-date will be appended to the original commit message.

## Usage
    Usage: svn2svn.py [-a] [-c] [-r SVN rev] <Source SVN URL> <Target SVN URL>
    
    Options:
      -h, --help            show this help message and exit
      -a, --keep-author     Keep revision Author or not
      -c, --continue-from-break
                            Continue from previous break
      -r SVN_REV, --svn-rev=SVN_REV
                            SVN revision to checkout from

## License
GPLv2, the same as hgsvn.

This project is a forked version of this svn2svn project:
**[http://code.google.com/p/svn2svn/](http://code.google.com/p/svn2svn/)**
