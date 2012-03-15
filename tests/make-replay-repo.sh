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
# Add pre-revprop-change hook script
cp ../hook-examples/pre-revprop-change_example.txt $REPO/hooks/pre-revprop-change
chmod 755 $REPO/hooks/pre-revprop-change
echo ""

## svn2svn /
#../svn2svn.py $* file://$PWD/_repo_ref file://$PWD/_repo_replay

# svn2svn /trunk
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
../svn2svn.py $* file://$PWD/_repo_ref/trunk file://$PWD/_repo_replay/trunk

## svn2svn /trunk/Module2/ProjectB
#svn mkdir -q -m "Add /trunk" $REPOURL/trunk
#svn mkdir -q --parents -m "Add /trunk/Module2/ProjectB" $REPOURL/trunk/Module2/ProjectB
#../svn2svn.py $* file://$PWD/_repo_ref/trunk/Module2/ProjectB file://$PWD/_repo_replay/trunk/Module2/ProjectB
