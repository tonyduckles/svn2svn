#!/bin/bash

test_description='Test diff-repo.sh
'
. ./test-lib.sh

PWD=$(pwd)
PWDURL=$(echo "file://$PWD" | sed 's/\ /%20/g')
REPO="$PWD/_repo_tmp"
REPO2="$PWD/_repo_tmp2"
REPOURL="$PWDURL/_repo_tmp"
REPO2URL="$PWDURL/_repo_tmp2"
WC="$PWD/_wc_tmp"

rm -rf "$REPO" "$REPO2" "$WC"

# Create dummy repo
svnadmin create "$REPO"
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
svn co -q $REPOURL/trunk "$WC"
mkdir -p "$WC/Module/ProjectA"
echo "Module/ProjectA/FileA1.txt (Initial)" >> "$WC/Module/ProjectA/FileA1.txt"
echo "Module/ProjectA/FileA2.txt (Initial)" >> "$WC/Module/ProjectA/FileA2.txt"
svn -q add "$WC/Module"
svn ci -q -m "Initial population" "$WC"

test_expect_success \
    "diff-repo: REPO1/trunk == REPO1/trunk" \
    "./diff-repo.sh $REPOURL/trunk $REPOURL/trunk"

test_expect_failure \
    "diff-repo: REPO1/trunk != REPO1/trunk/Module" \
    "./diff-repo.sh $REPOURL/trunk $REPOURL/trunk/Module"

rsync -aq $PWD/_repo_tmp/ $PWD/_repo_tmp2

test_expect_success \
    "diff-repo: REPO1/trunk == REPO2/trunk" \
    "./diff-repo.sh $REPOURL/trunk $REPO2URL/trunk"

rm -rf "$WC"
svn co -q $REPO2URL/trunk "$WC"
echo "Module/ProjectA/FileA1.txt (Edit)" >> "$WC/Module/ProjectA/FileA1.txt"
echo "Module/ProjectA/FileA3.txt (New File)" >> "$WC/Module/ProjectA/FileA3.txt"
svn -q add "$WC/Module/ProjectA/FileA3.txt"
svn ci -q -m "Second commit" "$WC"

test_expect_failure \
    "diff-repo: REPO1/trunk == REPO2/trunk" \
    "./diff-repo.sh $REPOURL/trunk $REPO2URL/trunk"

rm -rf "$REPO" "$REPO2" "$WC"
test_done
