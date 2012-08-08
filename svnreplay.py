#!/usr/bin/env python
import sys
from svn2svn.run.svnreplay import main

sys.exit(main() or 0)
