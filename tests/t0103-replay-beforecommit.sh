#!/bin/bash

test_description='Use svnreplay along with --pre-commit to create a modified filtered repo with only /trunk/Module2/ProjectB history
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
OFFSET="/trunk/Module2/ProjectB"
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
svn mkdir -q --parents -m "Add $OFFSET" $REPOURL$OFFSET

test_expect_success \
    "svnreplay --pre-commit _repo_ref$OFFSET _repo_replay$OFFSET" \
    "$SVNREPLAY -av --pre-commit=\"$PWD/t0103/before-commit.sh\" \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

test_expect_failure \
    "svnreplay --pre-commit _repo_ref$OFFSET _repo_replay$OFFSET (verify-all)" \
    "$SVNREPLAY -avcX --pre-commit=\"$PWD/t0103/before-commit.sh\" \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

test_expect_failure \
    "diff-repo _repo_ref$OFFSET _repo_replay$OFFSET" \
    "./diff-repo.sh \"$PWDURL/_repo_ref$OFFSET\" \"$PWDURL/_repo_replay$OFFSET\""

#rm -rf "$REPO" _wc_target
test_done
