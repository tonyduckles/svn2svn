"""
Exception sub-hierarchy:

RuntimeError
 +-- ExternalCommandFailed
 +-- SVNError
      +-- UnsupportedSVNFeature
      |    +-- UnsupportedSVNAction
      +-- SVNOutputError
           +-- EmptySVNLog
"""

class ExternalCommandFailed(RuntimeError):
    """
    An external command failed.
    """

class SVNError(RuntimeError):
    """
    A generic svn error.
    """

class UnsupportedSVNFeature(SVNError):
    """
    An unsuppported SVN (mis)feature.
    """

class UnsupportedSVNAction(UnsupportedSVNFeature):
    """
    An unknown/unsupported SVN action in an SVN log entry.
    """

class SVNOutputError(SVNError):
    """
    A generic error with the output of an SVN command.
    """

class EmptySVNLog(SVNOutputError):
    """
    An empty SVN log entry.
    """
