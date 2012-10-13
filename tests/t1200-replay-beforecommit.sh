#!/bin/bash

test_description='Use svnreplay along with --pre-commit to create a modified filtered repo with only /trunk/Module2/ProjectB history
'
. ./test-lib.sh
. ./replay-lib.sh

author='Tony Duckles <tony@nynim.org>'


SVNREPLAY="../svnreplay.py"
PWD=${TEST_DIRECTORY:-.}
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPONAME="_repo_t1200"
REPO="$PWD/$REPONAME"
REPOURL=$(echo "file://$REPO" | sed 's/\ /%20/g')
WC="$PWD/_wc_t1200"
OFFSET="/trunk/Module2/ProjectB"

test_expect_success \
    "pre-cleanup" \
    "rm -rf \"$WC\""

test_expect_success \
    "init repo $REPONAME" \
    "init_replay_repo \"$REPO\""

test_expect_success \
    "svn mkdir $REPONAME/trunk" \
    "svn mkdir -q -m \"Add /trunk\" $REPOURL/trunk"
test_expect_success \
    "svn mkdir $REPONAME$OFFSET" \
    "svn mkdir -q --parents -m \"Add $OFFSET\" $REPOURL$OFFSET"

test_expect_success \
    "svnreplay _repo_ref$OFFSET $REPONAME$OFFSET" \
    "$SVNREPLAY -av --pre-commit=\"$PWD/t1200/before-commit.sh\" --wc \"$WC\" \"$PWDURL/_repo_ref$OFFSET\" \"$REPOURL$OFFSET\""

test_expect_failure \
    "svnreplay _repo_ref$OFFSET $REPONAME$OFFSET (verify-all)" \
    "$SVNREPLAY -avcX --pre-commit=\"$PWD/t1200/before-commit.sh\" --wc \"$WC\" \"$PWDURL/_repo_ref$OFFSET\" \"$REPOURL$OFFSET\""

test_expect_failure \
    "diff-repo _repo_ref$OFFSET $REPONAME$OFFSET" \
    "./diff-repo.sh \"$PWDURL/_repo_ref$OFFSET\" \"$REPOURL$OFFSET\""

test_expect_success \
    "cleanup $REPONAME" \
    "rm -rf \"$REPO\" \"$WC\""

test_done
