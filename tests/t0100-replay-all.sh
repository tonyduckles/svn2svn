#!/bin/bash

test_description='Use svnreplay to create a full copy of the ref repo
'
. ./test-lib.sh

SVNREPLAY="../svnreplay.py"
PWD=$(pwd)
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPO="$PWD/_repo_replay"
REPOURL=$(echo "file://$REPO" | sed 's/\ /%20/g')

# Clean-up
rm -rf "$REPO" _wc_target

# Init repo
svnadmin create "$REPO"
# Add pre-revprop-change hook script
cp ../hook-examples/pre-revprop-change_example.txt "$REPO/hooks/pre-revprop-change"
chmod 755 "$REPO/hooks/pre-revprop-change"


################################################################
OFFSET="/"

test_expect_success \
    "svnreplay _repo_ref$OFFSET _repo_replay$OFFSET" \
    "$SVNREPLAY -a \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

test_expect_success \
    "diff-repo _repo_ref$OFFSET _repo_replay$OFFSET" \
    "./diff-repo.sh \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

test_done
