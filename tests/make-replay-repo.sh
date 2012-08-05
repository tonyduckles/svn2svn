#!/bin/bash
# Use svnreplay to create a filtered repo with only /trunk history

PWD=$(pwd)
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPO="$PWD/_repo_replay"
REPOURL=$(echo "file://$REPO" | sed 's/\ /%20/g')

# Clean-up
echo "Cleaning-up..."
rm -rf "$REPO" _wc_target

# Init repo
echo "Creating _repo_replay..."
svnadmin create "$REPO"
# Add pre-revprop-change hook script
cp ../hook-examples/pre-revprop-change_example.txt "$REPO/hooks/pre-revprop-change"
chmod 755 "$REPO/hooks/pre-revprop-change"
echo ""

## svnreplay /
#../svnreplay.py $* $PWDURL/_repo_ref $PWDURL/_repo_replay

# svnreplay /trunk
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
../svnreplay.py $* $PWDURL/_repo_ref/trunk $PWDURL/_repo_replay/trunk

## svnreplay /trunk/Module2/ProjectB
#svn mkdir -q -m "Add /trunk" $REPOURL/trunk
#svn mkdir -q --parents -m "Add /trunk/Module2/ProjectB" $REPOURL/trunk/Module2/ProjectB
#../svnreplay.py $* $PWDURL/_repo_ref/trunk/Module2/ProjectB $PWDURL/_repo_replay/trunk/Module2/ProjectB
