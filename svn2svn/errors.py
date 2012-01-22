
"""
Exception sub-hierarchy:

RuntimeError
 +-- ExternalCommandFailed
 +-- CommitCancelled
 +-- HgSVNError
      +-- UnsupportedSVNFeature
      |    +-- OverwrittenSVNBranch
      |    +-- UnsupportedSVNAction
      +-- SVNOutputError
           +-- EmptySVNLog

"""

class ExternalCommandFailed(RuntimeError):
    """
    An external command failed.
    """

class HgSVNError(RuntimeError):
    """
    A generic hgsvn error.
    """

class UnsupportedSVNFeature(HgSVNError):
    """
    An unsuppported SVN (mis)feature.
    """

class OverwrittenSVNBranch(UnsupportedSVNFeature):
    """
    The current SVN branch was overwritten with another one.
    """

class UnsupportedSVNAction(UnsupportedSVNFeature):
    """
    An unknown/unsupported SVN action in an SVN log entry.
    """

class SVNOutputError(HgSVNError):
    """
    A generic error with the output of an SVN command.
    """

class EmptySVNLog(SVNOutputError):
    """
    An empty SVN log entry.
    """
