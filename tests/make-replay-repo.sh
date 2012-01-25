#!/bin/sh
# Use svn2svn.py to create a filtered repo with only /trunk history

PWD=$(pwd)
REPO="$PWD/_repo_replay"
REPOURL="file://$REPO"

# Clean-up
echo "Cleaning-up..."
rm -rf $REPO _wc_target

# Init repo
echo "Creating _repo_replay..."
svnadmin create $REPO
echo ""

## svn2svn /
#../svn2svn.py -a -v file://$PWD/_repo_ref file://$PWD/_repo_replay

# svn2svn /trunk
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
../svn2svn.py -a -v file://$PWD/_repo_ref/trunk file://$PWD/_repo_replay/trunk
