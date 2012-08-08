#!/usr/bin/env python
import sys
from svn2svn.run.svnancest import main

sys.exit(main() or 0)
