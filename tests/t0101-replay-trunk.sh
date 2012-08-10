#!/bin/bash

test_description='Use svnreplay to create a filtered repo with only /trunk history
'
. ./test-lib.sh
. ./replay-lib.sh

SVNREPLAY="../svnreplay.py"
PWD=${TEST_DIRECTORY:-.}
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPO="$PWD/_repo_replay"
REPOURL=$(echo "file://$REPO" | sed 's/\ /%20/g')

init_replay_repo "$REPO"
rm -rf _wc_target

################################################################
OFFSET="/trunk"
svn mkdir -q -m "Add $OFFSET" $REPOURL$OFFSET

test_expect_success \
    "svnreplay _repo_ref$OFFSET _repo_replay$OFFSET" \
    "$SVNREPLAY -a \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

test_expect_success \
    "diff-repo _repo_ref$OFFSET _repo_replay$OFFSET" \
    "./diff-repo.sh \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

rm -rf "$REPO" _wc_target
test_done
