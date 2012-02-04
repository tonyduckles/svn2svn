#!/bin/sh
# Create a reference repo with both /trunk and /branches history

show_last_commit() {
    LOG=$(svn log -l1 $REPOURL)
    revision=$(echo "$LOG" | head -n 2 | tail -n 1| cut -d \| -f 1)
    comment=$(echo "$LOG" | head -n 4 | tail -n 1)
    _WC="${WC//\//\\/}"
    if [ -x $WC ]; then
        len=$(expr ${#REPOURL} + 7)
        url=$(svn info $WC | grep "URL:" | cut -c$len-)
        url="($url)"
    fi
    printf "%-6s%-22s%s\n" "$revision" "$url" "$comment"
}

svn_commit() {
    svn ci -q -m "$1" $2
    svn up -q
    show_last_commit
}


PWD=$(pwd)
REPO="$PWD/_repo_ref"
REPOURL="file://$REPO"
WC="$PWD/_wc_ref"

# Init repo
rm -rf $REPO $WC
echo "Creating _repo_ref..."
svnadmin create $REPO
svn mkdir -q -m "Add /trunk" $REPOURL/trunk
show_last_commit
svn mkdir -q -m "Add /branches" $REPOURL/branches
show_last_commit
TRUNK="$REPOURL/trunk"
svn co -q $TRUNK $WC
cd $WC

# Initial Population
mkdir -p $WC/Module/ProjectA
echo "Module/ProjectA/FileA1.txt (Initial)" >> $WC/Module/ProjectA/FileA1.txt
echo "Module/ProjectA/FileA2.txt (Initial)" >> $WC/Module/ProjectA/FileA2.txt
svn -q add $WC/Module
svn_commit "Initial population"

# Test #1: Add new file
# * Test simple copy-from branch
BRANCH="$REPOURL/branches/test1"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
mkdir -p $WC/Module/ProjectB
echo "Module/ProjectB/FileB1.txt (Test 1)" >> $WC/Module/ProjectB/FileB1.txt
svn add -q $WC/Module/ProjectB
svn_commit "Test 1: Add Module/ProjectB"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn ci -q --with-revprop 'testprop=Test 1 message' -m "Test 1: Add Module/ProjectB"
svn up -q
show_last_commit

# Empty commit message
echo "Module/ProjectB/FileB1.txt (Test 1.1)" >> $WC/Module/ProjectB/FileB1.txt
svn_commit ""

# Test #2: Rename files
# * Test rename support
# * Test committing rename in two different branch commits: first deletion, then add
BRANCH="$REPOURL/branches/test2"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectA/FileA2.txt Module/ProjectB/FileB2.txt
echo "Module/ProjectB/FileB2.txt (Test 2)" >> $WC/Module/ProjectB/FileB2.txt
svn_commit "Test 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB2.txt (part 1 of 2)" Module/ProjectA
svn_commit "Test 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB2.txt (part 2 of 2)" Module/ProjectB
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 2: Rename Module/ProjectA/FileA2.txt -> Module/ProjectB/FileB3.txt"

# Test #3: Verify rename
BRANCH="$REPOURL/branches/test3"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectB/FileB2.txt (Test 3)" >> $WC/Module/ProjectB/FileB2.txt
svn_commit "Test 3: Verify Module/ProjectB/FileB2.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 3: Verify Module/ProjectB/FileB2.txt"

# Test #4: Replace files
# * Test replace support
BRANCH="$REPOURL/branches/test4"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn rm -q Module/ProjectA/FileA1.txt
echo "Module/ProjectA/FileA1.txt (Test 4 - Replaced)" >> $WC/Module/ProjectA/FileA1.txt
svn add -q Module/ProjectA/FileA1.txt
svn_commit "Test 4: Replace Module/ProjectA/FileA1.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 4: Replace Module/ProjectA/FileA1.txt"

# Test #5: Rename files + folders
# * Test rename support
# * Create complex find-ancestors case, where files are renamed within a renamed folder on a branch
BRANCH="$REPOURL/branches/test5"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectB Module/ProjectC
svn mv -q Module/ProjectC/FileB1.txt Module/ProjectC/FileC1.txt
echo "Module/ProjectC/FileC1.txt (Test 5)" >> $WC/Module/ProjectC/FileC1.txt
svn mv -q Module/ProjectC/FileB2.txt Module/ProjectC/FileC2.txt
echo "Module/ProjectC/FileC2.txt (Test 5)" >> $WC/Module/ProjectC/FileC2.txt
svn_commit "Test 5: Rename Module/ProjectB -> Module/ProjectC"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 5: Rename Module/ProjectB -> Module/ProjectC"

# Test #6: Verify rename
BRANCH="$REPOURL/branches/test6"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectC/FileC1.txt (Test 6)" >> $WC/Module/ProjectC/FileC1.txt
echo "Module/ProjectC/FileC2.txt (Test 6)" >> $WC/Module/ProjectC/FileC2.txt
svn_commit "Test 6: Verify Module/ProjectC/FileC*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 6: Verify Module/ProjectC/FileC*.txt"

# Test #7: Rename files
# * Test rename support
# * Rename multiple files in the same folder
BRANCH="$REPOURL/branches/test7"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectC/FileC1.txt Module/ProjectC/FileC3.txt
echo "Module/ProjectC/FileC3.txt (Test 7)" >> $WC/Module/ProjectC/FileC3.txt
svn mv -q Module/ProjectC/FileC2.txt Module/ProjectC/FileC4.txt
echo "Module/ProjectC/FileC4.txt (Test 7)" >> $WC/Module/ProjectC/FileC4.txt
svn_commit "Test 7: Rename Module/ProjectC/FileC*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 7: Rename Module/ProjectC/FileC*.txt"

# Test #8: Verify rename
BRANCH="$REPOURL/branches/test8"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectC/FileC3.txt (Test 8)" >> $WC/Module/ProjectC/FileC3.txt
echo "Module/ProjectC/FileC4.txt (Test 8)" >> $WC/Module/ProjectC/FileC4.txt
svn_commit "Test 8: Verify Module/ProjectC/FileC*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 8: Verify Module/ProjectC/FileC*.txt"

# Test #9: Copy from older revision
svn copy -q -r 8 $TRUNK/Module/ProjectA/FileA2.txt@8 $WC/Module/ProjectA/FileA2.txt
svn propdel -q svn:mergeinfo Module/ProjectA/FileA2.txt
svn_commit "Test 9: Restore Module/ProjectA/FileA2.txt"

# Test #10: Verify copy
BRANCH="$REPOURL/branches/test10"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectA/FileA2.txt (Test 10)" >> $WC/Module/ProjectA/FileA2.txt
svn_commit "Test 10: Verify Module/ProjectA/FileA2.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 10: Verify Module/ProjectA/FileA2.txt"

# Test #11: Rename files + folders, multiple chained renames
# * Test rename support
# * Create complicated find-ancestors case, where files/folders are renamed multiple times on branch
BRANCH="$REPOURL/branches/test11"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mv -q Module/ProjectC Module/ProjectD
svn mv -q Module/ProjectD/FileC3.txt Module/ProjectD/FileD1.txt
echo "Module/ProjectD/FileD1.txt (Test 11)" >> $WC/Module/ProjectD/FileD1.txt
svn mv -q Module/ProjectD/FileC4.txt Module/ProjectD/FileD2.txt
echo "Module/ProjectD/FileD2.txt (Test 11)" >> $WC/Module/ProjectD/FileD2.txt
svn_commit "Test 11: Rename Module/ProjectC -> Module/ProjectD (part 1 of 2)" Module/ProjectC Module/ProjectD/FileC3.txt Module/ProjectD/FileC4.txt
svn_commit "Test 11: Rename Module/ProjectC -> Module/ProjectD (part 2 of 2)"
BRANCH="$REPOURL/branches/test11-1"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn merge -q $REPOURL/branches/test11
svn_commit "Test 11: Re-branch"
svn mv -q Module/ProjectD Module/ProjectE
svn mv -q Module/ProjectE/FileD1.txt Module/ProjectE/FileE1.txt
echo "Module/ProjectE/FileE1.txt (Test 11-1)" >> $WC/Module/ProjectE/FileE1.txt
svn mv -q Module/ProjectE/FileD2.txt Module/ProjectE/FileE2.txt
echo "Module/ProjectE/FileE2.txt (Test 11-1)" >> $WC/Module/ProjectE/FileE2.txt
svn_commit "Test 11: Rename Module/ProjectD -> Module/ProjectE (part 1 of 2)" Module/ProjectD Module/ProjectE/FileD1.txt Module/ProjectE/FileD2.txt
svn_commit "Test 11: Rename Module/ProjectD -> Module/ProjectE (part 2 of 2)"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 11: Rename Module/ProjectC -> Module/ProjectE"

# Test #12: Verify renames
BRANCH="$REPOURL/branches/test12"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectE/FileE1.txt (Test 12)" >> $WC/Module/ProjectE/FileE1.txt
echo "Module/ProjectE/FileE2.txt (Test 12)" >> $WC/Module/ProjectE/FileE2.txt
svn_commit "Test 12: Verify Module/ProjectE/FileE*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 12: Verify Module/ProjectE/FileE*.txt"

# Test #13: Replaces and add's inside a parent renamed folder.
BRANCH="$REPOURL/branches/test13"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn copy -q Module/ProjectA Module/ProjectB
echo "Module/ProjectB/FileA1.txt (Test 13-1)" >> $WC/Module/ProjectB/FileA1.txt
echo "Module/ProjectB/FileA2.txt (Test 13-1)" >> $WC/Module/ProjectB/FileA2.txt
svn_commit "Test 13: Copy Module/ProjectA -> Module/ProjectB"
svn mv -q Module/ProjectB/FileA1.txt Module/ProjectB/FileB1.txt
echo "Module/ProjectB/FileB1.txt (Test 13-2)" >> $WC/Module/ProjectB/FileB1.txt
svn mv -q Module/ProjectB/FileA2.txt Module/ProjectB/FileB2.txt
echo "Module/ProjectB/FileB2.txt (Test 13-2)" >> $WC/Module/ProjectB/FileB2.txt
svn_commit "Test 13: Rename Module/ProjectB/FileA*.txt -> FileB*.txt"
svn copy -q Module/ProjectB/FileB2.txt Module/ProjectB/FileB3.txt
echo "Module/ProjectB/FileB3.txt (Test 13-3)" >> $WC/Module/ProjectB/FileB3.txt
svn rm -q Module/ProjectB/FileB1.txt
echo "Module/ProjectB/FileB1.txt (Test 13-3 - Replaced)" >> $WC/Module/ProjectB/FileB1.txt
svn add -q Module/ProjectB/FileB1.txt
svn_commit "Test 13: Edits to Module/ProjectB/FileB*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 13: Create Module/ProjectB from Module/ProjectA"

# Test #14: Verify renames
BRANCH="$REPOURL/branches/test14"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectB/FileB1.txt (Test 14)" >> $WC/Module/ProjectB/FileB1.txt
echo "Module/ProjectB/FileB2.txt (Test 14)" >> $WC/Module/ProjectB/FileB2.txt
echo "Module/ProjectB/FileB3.txt (Test 14)" >> $WC/Module/ProjectB/FileB3.txt
svn_commit "Test 14: Verify Module/ProjectB/FileB*.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 14: Verify Module/ProjectB/FileB*.txt"

# Test #15: Replace copy-from
BRANCH="$REPOURL/branches/test15"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn rm -q Module/ProjectB/FileB2.txt
svn copy -q -r 22 $TRUNK/Module/ProjectC/FileC1.txt@22 Module/ProjectB/FileB2.txt
echo "Module/ProjectB/FileB2.txt (Test 15 - Replaced)" >> $WC/Module/ProjectB/FileB2.txt
svn_commit "Test 15: Replace Module/ProjectB/FileB2.txt from earlier Module/ProjectC/FileC1.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 15: Replace Module/ProjectB/FileB2.txt from earlier Module/ProjectC/FileC1.txt"

# Test #16: Verify replace
BRANCH="$REPOURL/branches/test16"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
echo "Module/ProjectB/FileB2.txt (Test 16)" >> $WC/Module/ProjectB/FileB2.txt
svn_commit "Test 16: Verify Module/ProjectB/FileB2.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 16: Verify Module/ProjectB/FileB2.txt"

# Test #17: Copy-from replaces and add's inside top-level initial-add folder
BRANCH="$REPOURL/branches/test17"
svn copy -q -m "Create branch" $TRUNK $BRANCH
svn switch -q $BRANCH
show_last_commit
svn mkdir -q Module2
svn copy -q Module/ProjectB Module2/ProjectB
echo "Module2/ProjectB/FileB1.txt (Test 17-1)" >> $WC/Module2/ProjectB/FileB1.txt
echo "Module2/ProjectB/FileB2.txt (Test 17-1)" >> $WC/Module2/ProjectB/FileB2.txt
echo "Module2/ProjectB/FileB3.txt (Test 17-1)" >> $WC/Module2/ProjectB/FileB3.txt
svn_commit "Test 17: Copy Module/ProjectB -> Module2/ProjectB"
svn rm -q Module2/ProjectB/FileB1.txt
svn copy -q -r 22 $TRUNK/Module/ProjectC/FileC2.txt@22 Module2/ProjectB/FileB1.txt
echo "Module2/ProjectB/FileB1.txt (Test 17-2)" >> $WC/Module2/ProjectB/FileB1.txt
svn_commit "Test 17: Replace Module2/ProjectB/FileB1.txt from earlier Module/ProjectC/FileC2.txt"
svn switch -q $TRUNK
svn merge -q $BRANCH
svn_commit "Test 17: Create Module2/ProjectB from Module/ProjectB"

# Clean-up
echo "Cleaning-up..."
rm -rf $WC
