#!/bin/sh
# Use svn2svn.py to create a filtered repo with only /trunk history

PWD=$(pwd)
REPO="$PWD/_repo_replay"
REPOURL="file://$REPO"

# Clean-up
echo "Cleaning-up..."
rm -rf $REPO _dup_wc

# Init repo
echo "Creating _repo_replay..."
svnadmin create $REPO
svn mkdir -q -m "Add /trunk" $REPOURL/trunk

# svn2svn
../svn2svn.py -a file://$PWD/_repo_ref/trunk file://$PWD/_repo_replay/trunk
