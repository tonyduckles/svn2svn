#!/bin/bash

test_description='Test diff-repo.sh
'
. ./test-lib.sh

author='Tony Duckles <tony@nynim.org>'


PWD=${TEST_DIRECTORY:-.}
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPONAME="_repo_t0100"
REPO1="$PWD/${REPONAME}_tmp1"
REPO2="$PWD/${REPONAME}_tmp2"
REPO1URL="$PWDURL/_repo_t0100_tmp1"
REPO2URL="$PWDURL/_repo_t0100_tmp2"
WC="$PWD/_wc_t0100"

test_expect_success \
    "pre-cleanup" \
    "rm -rf \"$REPO1\" \"$REPO2\" \"$WC\""

test_expect_success \
    "create repo1" \
    "svnadmin create \"$REPO1\""

test_expect_success \
    "populate repo1" \
    "svn mkdir -q -m \"Add /trunk\" $REPO1URL/trunk && \
     svn co -q $REPO1URL/trunk \"$WC\" && \
     mkdir -p \"$WC/Module/ProjectA\" && \
     echo \"Module/ProjectA/FileA1.txt (Initial)\" >> \"$WC/Module/ProjectA/FileA1.txt\" && echo \"Module/ProjectA/FileA2.txt (Initial)\" >> \"$WC/Module/ProjectA/FileA2.txt\" && \
     svn -q add \"$WC/Module\" && \
     svn ci -q -m \"Initial population\" \"$WC\""

test_expect_success \
    "diff-repo: repo1/trunk == repo1/trunk" \
    "./diff-repo.sh $REPO1URL/trunk $REPO1URL/trunk"

test_expect_failure \
    "diff-repo: repo1/trunk != repo1/trunk/Module" \
    "./diff-repo.sh $REPO1URL/trunk $REPO1URL/trunk/Module"

test_expect_success \
    "rsync repo1 -> repo2" \
    "rsync -aq \"$REPO1/\" \"$REPO2\""

test_expect_success \
    "diff-repo: repo1/trunk == repo2/trunk (identical)" \
    "./diff-repo.sh $REPO1URL/trunk $REPO2URL/trunk"

test_expect_success \
    "modify repo2 content" \
    "rm -rf \"$WC\" && \
     svn co -q $REPO2URL/trunk \"$WC\" && \
     echo \"Module/ProjectA/FileA1.txt (Edit)\" >> \"$WC/Module/ProjectA/FileA1.txt\" && \
     echo \"Module/ProjectA/FileA3.txt (New File)\" >> \"$WC/Module/ProjectA/FileA3.txt\" && \
     svn -q add \"$WC/Module/ProjectA/FileA3.txt\" && \
     svn ci -q -m \"Second commit\" \"$WC\""

test_expect_failure \
    "diff-repo: repo1/trunk != repo2/trunk (changed)" \
    "./diff-repo.sh $REPO1URL/trunk $REPO2URL/trunk"

test_expect_success \
    "cleanup" \
    "rm -rf \"$REPO1\" \"$REPO2\" \"$WC\""

test_done
